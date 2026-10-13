"""Authorization for the control plane (WS0.6, ARCH-348).

authN is already solved (Cloudflare Access verified JWT → a trusted email, in
access_identity). This module is authZ only: given a resolved identity, may it
take an action on a product-scoped resource? Deny-by-default.

The model generalizes tiers.json's binary full/contractor into per-email roles
(roles.json, gitignored, re-read per request so a grant takes effect with no
restart — the same no-restart ethos as tiers.json) plus a product scope that
roles.json no longer carries: `human_identity` takes it as a parameter,
resolved by the caller through the Library (`library_client.person_scope`,
E19·1 issue_01M35K54QHJYYPXWS28YWEHJP3) rather than read from a local file —
Orient holds no table, file or constant mapping a person to their projects.
roles.json's CONTENTS are still access-control data: never log them or paste
them into a PR body.

The load-bearing invariant, encoded as code rather than convention: an AGENT may
create, consume, and comment on decisions, but only a HUMAN identity may set a
terminal approve/deny/redirect. The ROLE_CAPS matrix below simply omits "decide"
from the agent capability set, so an agent token calling the decide route is
denied by the same gate that authorizes everything else — the human-in-the-loop
contract (ARCH-020) made unbypassable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from paths import CONFIG_ROOT

ROLES = CONFIG_ROOT / "roles.json"


@dataclass(frozen=True)
class Identity:
    """Who is acting. `actor` is the audit id (a human's verified email or an
    agent's name). `kind` drives the decide gate; `products` is the scope
    (frozenset, "*" = all products)."""
    actor: str
    kind: str  # "human" | "agent" | "system"
    role: str  # human: admin | product_owner | member | contractor ; agent: "agent" ; system: "system"
    products: frozenset[str] = field(default_factory=frozenset)
    queues: frozenset[str] = field(default_factory=frozenset)


# Capability per effective role. "decide" is deliberately absent from "agent" and
# "contractor": an agent can never set a terminal decision, a contractor is
# read-only. Deny-by-default falls out of this — an unknown role maps to an empty
# capability set.
# Signal-memory capabilities (S0, ARCH-349) are DISTINCT from the decision
# read/create caps on purpose: a contractor holds decision "read" (the read-tier
# bundle) but must NOT thereby read the signal store, so signals get their own
# signal_read / signal_write. signal_customer is the elevated cap for System-B
# customer-authored content (privacy_class='customer') — held by the internal team
# (admin, member, observer) and internal system jobs, never a contractor and never
# a generic agent (a coding agent has no business reading customer PII).
# "observer" (ARCH-351) is the full-read internal role: everything readable,
# customer content included, with no write capability on either plane and no
# decide — an internal teammate who follows the work without owning a queue.
# Note signal_customer alone cannot write customer rows: the create route checks
# signal.create (→ signal_write) FIRST, which observer lacks.
# "contributor" (ARCH-351 follow-up, the operator 2026-07-16) is observer + signal_write:
# an internal teammate who also CAPTURES signals (uploads a transcript, logs a
# customer call — with signal_customer, customer-class capture included) but
# still holds no decision-plane writes and no decide.
# room_request (ARCH-357 concierge) is held by EVERY human role (a member editing
# their OWN room via the concierge), never by agent/system — the request box is a
# human surface, and the concierge itself writes rows as the server, not via this cap.
ROLE_CAPS: dict[str, frozenset[str]] = {
    "admin":      frozenset({"read", "create", "consume", "comment", "decide",
                             "signal_read", "signal_write", "signal_customer",
                             # provision/curate any room + edit own (ARCH-357)
                             "room_admin", "room_request", "blog_write",
                             "kb_write", "policy_write",
                             # approving an epic: admin and product_owner (the operator,
                             # 2026-09-22, "yes to admin only for now, we will need
                             # a product owner role too"; the role followed on
                             # 2026-09-30)
                             "epic_approve",
                             # choosing the model a queued work item runs on:
                             # admin-only. It shipped as the same grant as
                             # epic_approve; product_owner holds epic_approve
                             # but not this.
                             "item_model",
                             # alert thresholds: admin-only "for now" — asked plainly
                             # whether to open this to members, the operator said "administrators
                             # for now" (2026-09-16)
                             "alerts_policy_write",
                             # a project's ledger cap set — warn, soft cap, hard cap and
                             # per-run cap (issue_01M4BH8KSSMC8XBKB2C2CX9QTF). Admin-only,
                             # matching item_model and alerts_policy_write: a cap is what
                             # ends a run or refuses one at checkout, and nothing names a
                             # request to widen that past the operator.
                             "ledger_caps_write"}),
    "member":     frozenset({"read", "create", "consume", "comment", "decide",
                             "signal_read", "signal_write", "signal_customer",
                             "room_request", "blog_write", "kb_write"}),
    # A member who also approves the epics of the products in their
    # Library-resolved scope (the operator, 2026-09-30, agreeing to "member permissions
    # plus epic_approve, scoped to the person's own products";
    # issue_01M350CGZB6DR577FASP53K7WM). The scoping is epic.approve's own, so
    # nothing else here changes for it.
    #
    # RESERVED FOR A HUMAN OPERATOR, never an agent (the operator, same day: "product
    # owner is reserved for a human operator, it's not an agent role"). That
    # holds structurally: a role string reaches an identity only through
    # human_identity, from a verified CF Access email, and _effective_role
    # pins any agent- or system-kind identity to its own role whatever role it
    # carries. Never add this role to an agent token or a roster entry.
    "product_owner": frozenset({"read", "create", "consume", "comment", "decide",
                                "signal_read", "signal_write", "signal_customer",
                                "room_request", "blog_write", "kb_write",
                                "epic_approve"}),
    "observer":   frozenset({"read", "signal_read", "signal_customer",
                             "room_request"}),  # full read, no writes (but may edit own room)
    "contributor": frozenset({"read", "signal_read", "signal_write", "signal_customer",
                              "room_request"}),
    "contractor": frozenset({"read", "room_request"}),  # read-tier bundle + edit own room
    "agent":      frozenset({"read", "create", "consume", "comment",  # NEVER decide
                             "signal_read", "signal_write"}),
    "system":     frozenset({"read", "create", "consume", "comment",  # internal jobs, not decide
                             "signal_read", "signal_write", "signal_customer"}),
}

# Which capability each action needs. An action not in this map is unknown →
# denied (deny-by-default; a typo or a new route without a rule fails closed).
ACTION_CAP: dict[str, str] = {
    "decision.read": "read",
    "decision.create": "create",
    "decision.consume": "consume",
    "decision.comment": "comment",
    "decision.decide": "decide",
    "signal.read": "signal_read",
    "signal.create": "signal_write",
    "signal.read_customer": "signal_customer",
    "signal.create_customer": "signal_customer",
    # Reading Room (ARCH-357). room.read is the read-tier bundle every human role
    # holds (a member reads their OWN room; the render enforces grant ∩ curation).
    # room.admin provisions + curates rooms — admin only. room.request is the
    # concierge edit loop — every human role, own-room-only.
    "room.read": "read",
    "room.admin": "room_admin",
    "room.request": "room_request",
    # Blog authoring on the team site (ARCH-363). Deliberately its own capability
    # rather than riding "create": that one governs the decision plane, and a
    # person who may raise a decision is not thereby someone who may publish to a
    # public-facing site. Deny-by-default — only admin and member hold it.
    "blog.write": "blog_write",
    # Editing a knowledge-base document in the window (ARCH-... the option-C KB
    # editor, issue_01M0TKX6DW2E1TXGN9QT6K61J5 step 4). Its own capability for
    # the same reason blog_write is its own: this gates a direct, attributed
    # commit to a git-reviewed company repository, which is a different thing
    # from being able to raise a decision. Deny-by-default — only admin and
    # member hold it, matching blog_write; an agent or a contractor never does.
    "kb.write": "kb_write",
    # Approving an epic (issue_01M32CB6A9T04NVYPARGDT2GWQ; the operator, 2026-09-21:
    # "all epics stay as proposed ... until the operator presses the approve
    # button in either Deck or Orient"). Its own capability for the reason
    # blog_write and kb_write are: approving is the act that puts an epic on a
    # product's Course and commits the fleet to building it, which is a
    # different thing from being able to raise a decision or plan one.
    #
    # **PLANNING IS NOT APPROVING**, which is why this does NOT reuse the
    # `consume` that Course writes take (see the Course comment below). That
    # ruling is what makes a Course able to hold unapproved epics; this
    # capability is the other half of it, gating the act that changes their
    # standing.
    #
    # Granted to admin and product_owner (the operator, 2026-09-22: "yes to admin only
    # for now, we will need a product owner role too"; the role landed for
    # issue_01M350CGZB6DR577FASP53K7WM). It first shipped to admin and member,
    # following the approved specification's contrast against a contractor. A
    # product_owner approves only within its Library-resolved scope, because
    # in_scope checks the epic's own product.
    "epic.approve": "epic_approve",
    # Editing a PROPOSED epic's specification and definition of done from its
    # own page. The same capability as approving, deliberately: the text is what
    # an approval approves, so whoever may approve it may correct it first, and
    # a second grant would let the two drift into different sets of people.
    # Product-scoped for the same reason `epic.approve` is.
    "epic.revise": "epic_approve",
    # Choosing the model a queued work item runs on (issue_01M38WVA7XHEDN1NFNPP8KDM0C;
    # The operator, 2026-09-23: the button "changes the model on work already queued,
    # loudly"). Its own capability because the Bosun honours the choice on an
    # autonomous dispatch, where a typed override is refused, so it lifts the
    # roster's model pin for that item. Product-scoped, like epic.approve.
    "item.model": "item_model",
    # In-box issue queue (ARCH-359 step 2a; renamed ARCH-361). Reuses the existing read/create/consume
    # caps — no new capability, same as the room.* actions above — so a role's
    # issue-queue reach is exactly its decision-plane reach: claim and resolve both
    # map to "consume" (the same "an agent may act, only decide is human-gated"
    # shape, minus a decide step — an issue never needs human approval to progress).
    "issue.read": "read",
    "issue.create": "create",
    "issue.claim": "consume",
    "issue.resolve": "consume",
    # The Course (ADR-0029) reuses read/consume, minting no capability, for the
    # reason the issue actions above give.
    #
    # WRITING A COURSE IS `consume`, NOT `decide`, and that follows directly from
    # The operator's ruling that a Course may hold epics nobody has approved: planning is
    # not approving, and requiring approval before a thing may be planned is a
    # double gate on one decision. So a co recording the plan it just made is the
    # normal author, and gating it to humans would leave that plan where it is
    # today — in prose, in a session that ends.
    #
    # `consume` RATHER THAN `create` MAKES NO DIFFERENCE TODAY, and the choice is
    # recorded because of that rather than in spite of it. As of 2026-08-06 the
    # two capabilities are held by exactly the same roles — admin, member, agent,
    # system — so nothing observable separates them and no test can tell them
    # apart. What the choice does is pick which future split this follows: if a
    # role is ever added that may FILE work without ACTING on it, a Course is on
    # the acting side, because setting a project's build order is not something
    # someone who can only report a problem should be able to do.
    "course.read": "read",
    "course.write": "consume",
    # The thread (ARCH-373) reuses the existing `comment` capability rather than
    # minting one, in the same spirit as the caps above. It lands where it should
    # without further thought: `agent` holds it, so bug-investigator can post the
    # diagnosis that IS its deliverable; `observer` does not, so the full-read
    # internal role follows the work without writing to the handoff record.
    "issue.comment": "comment",
    # The agent tool deny-list write route (ADR-0048, epic issue_01M0C91CGDR0YT8T5ERX2HD2HS,
    # issue_01M1F1ENN8P6RD4W3SJJ04HR45). Its OWN capability rather than a
    # reuse of "room_admin"/"kb_write"/"blog_write", so it can be granted
    # deliberately, to whichever role the operator chooses, and re-checked a second
    # time against the crew a request actually resolves to (policy_api.py's
    # second authorize() call) — a check that only makes sense if the
    # capability is its own rather than borrowed.
    #
    # The operator corrected the reasoning behind this line on 2026-09-04. It used to
    # hold "policy_write" out of every role including "admin", on the theory
    # that "holding admin rights over documents must not carry the right to
    # change what an agent may do." That theory was circular: the tool list
    # IS a document, and in this system only documents change what an agent
    # may do, so the distinction it drew between "documents" and "what an
    # agent may do" does not exist. What actually excludes an agent is
    # policy_api.py's unconditional `ident.kind != "human"` check, which runs
    # FIRST, before this capability is ever consulted — no capability,
    # however narrow, substitutes for it. Withholding policy_write from
    # "admin" bought nothing against agents; it could only separate one human
    # role from another, and two mechanisms already make that separation
    # unnecessary here: a non-admin role's capability set is defined outright
    # in ROLE_CAPS rather than inherited from "admin"'s, so widening "admin"
    # widens nothing else; and `_effective_role` pins an agent- or
    # system-kind identity to its own role whatever role string it carries.
    # "admin" holds policy_write below for those reasons.
    #
    # WHO THAT ACTUALLY GRANTS, stated because the blast radius is the thing
    # a reader will want and the previous version of this sentence got it
    # wrong. In roles.json at the time of writing, `operator@example.com` is
    # "admin" and `contributor@example.com` is "contributor" — so this grants the
    # capability to one identity, not two. Both are full-tier, but that is
    # tiers.json's separate concept and does not confer a role; conflating
    # the two is the error this paragraph replaces. **Adding a second
    # "admin" grants them the deny-list write by default**, so if that is
    # ever not intended, the fix is a new role rather than reinstating the
    # withholding.
    #
    # Do not reinstate the withholding as a way to keep agents out of this
    # route — that job is already done, elsewhere, unconditionally, and
    # reads as this comment used to.
    "policy.write": "policy_write",
    # Dashboard alert-threshold writes (issue_01M17W6XGXVHNDWCFRTQ38Y2RK). Its
    # own capability rather than a reuse of blog_write/kb_write, matching
    # policy_write's own reasoning: this is a distinct surface the operator may want to
    # grant or withhold on its own terms, not "whatever admin/member already
    # hold". Admin-only, like policy_write: asked plainly whether this should
    # be open to members, the operator said "administrators for now" (2026-09-16).
    "alerts.write": "alerts_policy_write",
    # Setting a project's ledger cap set (issue_01M4BH8KSSMC8XBKB2C2CX9QTF; the
    # Library's ledger epic, approved-client-only on its own side as
    # `ledger.grant`). Its own capability rather than a reuse of item_model or
    # alerts_policy_write: this write reaches a different Library route and
    # governs a different thing — what refuses a checkout and ends a run, not
    # what a dashboard alerts on or what model a queued item runs on.
    # Product-scoped, like item.model and epic.approve: a cap set belongs to
    # one project (the ledger's `scope_id`), so a human scoped to one product
    # must not set another's caps.
    "ledger.caps_write": "ledger_caps_write",
}

# Same derivation for the /signals capture form's __CAN_CAPTURE__ toggle: a role
# can capture exactly when it holds signal_write. Cosmetic like __CAN_DECIDE__ —
# the POST /api/signals authz gate is the wall.
CAPTURE_ROLES = frozenset(r for r, caps in ROLE_CAPS.items() if "signal_write" in caps)

# Same derivation for the /engineering/issues "Log an issue" form's __CAN_LOG_ISSUE__ toggle: a role
# can log an issue exactly when it holds "create" (issue.create -> create). Cosmetic —
# the POST /api/issues authz gate is the wall.
ISSUE_LOGGER_ROLES = frozenset(r for r, caps in ROLE_CAPS.items() if "create" in caps)

# Same derivation for the Reading Room's blog-drafts panel __CAN_BLOG_WRITE__ toggle: a
# role can see the panel exactly when it holds blog_write (blog.write -> blog_write).
# Cosmetic — the blog_api authz gate on every write AND the list route is the wall.
BLOG_WRITER_ROLES = frozenset(r for r, caps in ROLE_CAPS.items() if "blog_write" in caps)

# Same derivation for the in-window KB document editor's __CAN_KB_WRITE__ toggle
# on /reference/doc/<id>: a role sees the edit affordance exactly when it holds
# kb_write (kb.write -> kb_write). Cosmetic — the kb_write_api authz gate on
# every write route is the wall; a viewer who lacks the capability but somehow
# posts to it anyway is refused there, not here.
KB_WRITER_ROLES = frozenset(r for r, caps in ROLE_CAPS.items() if "kb_write" in caps)

# Same derivation for the tool-policy page's write form __CAN_POLICY_WRITE__
# toggle: a role sees the affordance exactly when it holds policy_write
# (policy.write -> policy_write). Cosmetic — policy_api's own authz gate is
# the wall. Granted to "admin" only (see the ACTION_CAP comment above), so
# the form renders for admin and stays hidden for every other role.
POLICY_WRITER_ROLES = frozenset(r for r, caps in ROLE_CAPS.items() if "policy_write" in caps)

# Same derivation for the three dashboards' own alert-threshold write forms'
# __CAN_ALERTS_WRITE__ toggle: a role sees the affordance exactly when it
# holds alerts_policy_write (alerts.write -> alerts_policy_write). Cosmetic —
# alerts_policy_api's own authz gate is the wall. Granted to "admin" only (see
# the ACTION_CAP comment above), so the form renders for admin and stays
# hidden for every other role.
ALERTS_POLICY_WRITER_ROLES = frozenset(
    r for r, caps in ROLE_CAPS.items() if "alerts_policy_write" in caps)

# Same derivation for the Chart House's Approve control __CAN_APPROVE_EPIC__
# toggle: a role sees the affordance exactly when it holds epic_approve
# (epic.approve -> epic_approve). Cosmetic — approve_api's own authz gate, and
# the Library's own `epic.approve` capability behind it, are the wall. Granted
# to admin and product_owner, so the control renders for those two and stays
# hidden for every other role, member included. The toggle is per role, not per
# product: a product_owner sees Approve on every product's epics and is refused
# (404) on those outside their scope.
EPIC_APPROVER_ROLES = frozenset(r for r, caps in ROLE_CAPS.items() if "epic_approve" in caps)

# The same derivation for the item page's model control __CAN_SET_ITEM_MODEL__.
# Cosmetic — model_override_api's gate and the Library's `item.model` are the wall.
ITEM_MODEL_SETTER_ROLES = frozenset(r for r, caps in ROLE_CAPS.items() if "item_model" in caps)

# The same derivation for the Spending page's per-project ledger-caps form
# __CAN_SET_LEDGER_CAPS__. Cosmetic — ledger_caps_api's gate and the Library's
# own `ledger.grant` behind it are the wall. Admin-only, so the form renders
# for admin and stays hidden for every other role.
LEDGER_CAPS_WRITER_ROLES = frozenset(r for r, caps in ROLE_CAPS.items() if "ledger_caps_write" in caps)


def _effective_role(identity: Identity) -> str:
    """An agent's effective role is always 'agent' regardless of any role string,
    so an agent can never inherit human decide capability."""
    if identity.kind == "agent":
        return "agent"
    if identity.kind == "system":
        return "system"
    return identity.role


# Actions where product=None is in scope. Two different reasons land an action
# here, and both matter for the invariant NON_SCOPED_ACTIONS guards:
#  - a cross-product READ: the route reads across every product the identity is
#    scoped to and filters the results itself (decision.read, signal.read,
#    signal.read_customer, room.read, issue.read, course.read).
#  - a capability with NO PRODUCT DIMENSION AT ALL, so "product scope" doesn't
#    apply to it in the first place: a Reading Room is scoped to one member's
#    email, not a convivy-product (room.admin, room.request); the blog and the
#    KB are site-wide surfaces (blog.write, kb.write); policy.write's
#    identity-only gate runs before the request body is parsed, with the
#    crew-scoped recheck a SECOND, separate call to authorize() once the real
#    scope is known (policy_api.py's own comment explains why that second call
#    is not redundant) — the first call has no product to give it.
#
# Every action NOT listed here is a write to a genuinely product-scoped
# resource (a decision, a signal, an issue, a Course belong to one product),
# so product=None must never be a free pass for it — not even for an identity
# holding "*", since a create/consume/comment/decide action always needs a
# real product to act on. Unlisted here is the strict reading, the same
# direction as ACTION_CAP's own deny-by-default: a write action added to
# ACTION_CAP without also being added here stays scoped, not thrown open.
NON_SCOPED_ACTIONS: frozenset[str] = frozenset({
    "decision.read", "signal.read", "signal.read_customer", "room.read",
    "issue.read", "course.read",
    "room.admin", "room.request", "blog.write", "kb.write", "policy.write",
    # A dashboard alert threshold is a site-wide setting like blog.write/
    # kb.write — no convivy-product to scope it to.
    "alerts.write",
})


def in_scope(identity: Identity, action: str, product: str | None) -> bool:
    """A product-scoped resource is reachable when the identity holds "*" or that
    product. product=None is in scope only for NON_SCOPED_ACTIONS — a
    cross-product read that filters its own results, or a capability with no
    product dimension at all. For any other action, None never satisfies scope,
    however wide: even an identity holding "*" needs a real product to write a
    product-scoped resource."""
    if product is None:
        return action in NON_SCOPED_ACTIONS
    return "*" in identity.products or product in identity.products


def authorize(identity: Identity, action: str, product: str | None = None) -> bool:
    """The one gate. Deny-by-default: an unknown action, a role lacking the
    capability, or an out-of-scope product all return False."""
    cap = ACTION_CAP.get(action)
    if cap is None:
        return False  # unknown action → fail closed
    if cap not in ROLE_CAPS.get(_effective_role(identity), frozenset()):
        return False
    return in_scope(identity, action, product)


def _roles_lowercased() -> dict:
    """roles.json keyed by lowercased email. Absent/malformed → {} (no grants →
    everyone is a bare contractor with no write scope)."""
    try:
        raw = json.loads(ROLES.read_text())
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): v for k, v in raw.items()}


def feature_gate_actors() -> frozenset[str]:
    """One snapshot of the actors holding the explicit `feature-gate` grant —
    the humans whose own filing of a `type=feature` issue authorizes an autonomous
    overnight build (the 🚀 this replaces).

    A DEDICATED grant in roles.json `queues`, and deliberately NOT implied by the
    `"*"` wildcard: a second human promoted from contributor does not gain
    build-authorization until `"feature-gate"` is added to their entry by hand
    (ARCH-406 follow-up; the operator's decision dec_01KYAS3KPY2QMQX6DPYA694XFD — a second
    human's build authority must be an opt-in, not a side effect of a role change).
    Holding the grant already implies a roles.json entry, so this subsumes the
    membership check it supersedes: a removed identity holds no grant.

    Taken as a SNAPSHOT for the same reason the list path needs one — every row
    classified against one read of roles.json, so a mid-iteration write can't make
    one response say `human` for an actor early and `agent` for it late.
    """
    out = set()
    for email, entry in _roles_lowercased().items():
        if not isinstance(entry, dict):
            continue
        queues = entry.get("queues")
        # `queues` must be a list. A truthy non-iterable (`"queues": 42`) would
        # make `"feature-gate" in queues` raise and crash the whole response, so
        # anything that is not a list reads as no grant — the fail-safe direction.
        if isinstance(queues, list) and "feature-gate" in queues:
            out.add(email)
    return frozenset(out)


def holds_feature_gate(actor: str, granted: frozenset[str] | None = None) -> bool:
    """True iff `actor` holds the feature-gate grant. Pass `granted` from
    feature_gate_actors() when classifying many rows; omitted, it reads roles.json.

    An unrecognised or ungranted actor is NOT authorized — the safe direction,
    since this gates whether the overnight builds a feature request unattended.
    """
    if not actor:
        return False
    if granted is None:
        granted = feature_gate_actors()
    return actor.strip().lower() in granted


def human_identity(email: str, products: frozenset[str]) -> Identity:
    """Resolve a verified CF Access email to its identity. `products` is the
    viewer's project scope, resolved by the caller through the Library
    (`library_client.person_scope`, once per request beside the identity
    gate — server.py's `_identity`, or `api_common.resolve_identity` for the
    write APIs) — never read from roles.json (E19·1,
    issue_01M35K54QHJYYPXWS28YWEHJP3, comment 4450). roles.json still carries
    `role` and `queues`, which say what a person may do, not which projects
    they cover.

    No roles.json entry → a bare contractor (deny-by-default on the write
    plane; the read-tier bundle is still governed separately by tiers.json),
    carrying `products` as given — even a contractor's scope is the Library's
    to say, never widened or narrowed here."""
    entry = _roles_lowercased().get(email.strip().lower())
    if not isinstance(entry, dict):
        return Identity(actor=email, kind="human", role="contractor", products=products)
    role = entry.get("role", "contractor")
    if role not in ROLE_CAPS:
        role = "contractor"  # an unrecognized role grants nothing beyond read
    queues = frozenset(str(q) for q in entry.get("queues", []) if isinstance(q, str))
    return Identity(actor=email, kind="human", role=role, products=products, queues=queues)


def agent_identity(name: str, products: frozenset[str]) -> Identity:
    """An agent actor (resolved from its bearer token in WS0.7). Role is fixed to
    'agent' so the decide capability can never apply, whatever its scope."""
    return Identity(actor=name, kind="agent", role="agent", products=products)


def system_identity(name: str, products: frozenset[str]) -> Identity:
    """An internal PLATFORM job (resolved from a bearer token whose
    api-tokens.json entry carries "kind": "system" — ARCH-352). Holds
    signal_customer, which the agent role categorically lacks.

    **No job exercises that capability today, so read it as what the kind
    grants rather than as something that happens.** It was granted for the
    Gmail capture, which set privacy_class="customer" on every row it wrote;
    that job retired on 2026-09-16 when the operator moved capture to the Signals
    product. `github_capture` is the only system-kind job left, and it sets no
    privacy_class at all (its rows default to internal, signal_capture_api.py)
    and reads no signals — it only POSTs. The grant is left in place because
    removing a capability is a separate decision from retiring its one
    consumer; whoever takes that decision should start here.

    Still never decide (_effective_role pins kind=system → role=system). Grant
    this kind only to tokens for our own launchd jobs, never to a coding
    agent's token."""
    return Identity(actor=name, kind="system", role="system", products=products)
