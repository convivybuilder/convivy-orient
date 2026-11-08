"""The control-plane data model (WS0.5, ARCH-348).

Orient's own concerns only (ADR-0022): the Reading Room, Web Push
subscriptions, and dashboard alert policy. Field Notes posts live in the
Archives since CQ·170 (issue_01M5GBJ3H5BFX0CR83019T737D).
Everything else that used to live here — the decision plane, the issue queue,
signal memory, the Course — was a second copy of what the Library now owns
alone, and is gone as of
issue_01M08YKNV185DFJ6832GV8ZY22 (2026-08-19).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def utcnow() -> dt.datetime:
    """Naive UTC. SQLite has no native timezone, so the whole datastore stores
    and compares naive-UTC datetimes by convention; the API layer renders them
    with a trailing Z."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class ReadingRoom(Base):
    """One team member's personalized reading surface (ARCH-357). It holds the
    CURATION — which KB docs the room shows, ordered, with notes — never the
    GRANT (what the member may see), which is computed live from tiers.json +
    roles.json + audience frontmatter in kb_index.room_entitlement. A room render
    is always curation ∩ grant, so a curated entry outside the member's current
    grant simply drops out of the render; the room can neither store nor widen an
    entitlement. Provisioned on join, persists for the member's tenure (tenure end
    archives, never deletes)."""

    __tablename__ = "reading_rooms"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # room_<ulid>
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)  # CF Access identity link, lowercased
    title: Mapped[str] = mapped_column(String(300), default="")
    landing_note: Mapped[str] = mapped_column(Text, default="")  # markdown, rendered at the top of the room
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)  # active | archived
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    entries: Mapped[list["RoomEntry"]] = relationship(
        back_populates="room", cascade="all, delete-orphan", order_by="RoomEntry.id")


class RoomEntry(Base):
    """One curated entry in a member's room: a reference plus its presentation
    (section grouping, order, optional note, pinned). The room holds no content
    of its own beyond the note — `doc_id` is the referenced thing's own id, in
    whichever store `kind` names (ARCH-357, widened for CQ·147, E22):

      - kind="doc"    → a KB doc id, e.g. "alpha/runbooks/mini"
      - kind="item"   → a Chart House work item or epic id
      - kind="signal" → an Archives entry id

    Uniqueness on (room_id, kind, doc_id) makes one reference appear at most
    once per room, now that the same id text can in principle name a doc and
    an item without colliding. The render still intersects every entry against
    the live grant, so an entry is a curation intent, never an authorization to
    read."""

    __tablename__ = "room_entries"
    __table_args__ = (
        UniqueConstraint("room_id", "kind", "doc_id", name="uq_room_entry_kind_doc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("reading_rooms.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="doc")  # doc | item | signal
    doc_id: Mapped[str] = mapped_column(String(200))  # the referenced thing's own id
    section: Mapped[str] = mapped_column(String(120), default="")  # display grouping: "Start here", "Alpha"
    position: Mapped[int] = mapped_column(Integer, default=0)      # order within section
    note: Mapped[str | None] = mapped_column(Text)                 # per-entry annotation, markdown
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="steward")  # provision | request | steward
    added_by: Mapped[str] = mapped_column(String(200))            # audit actor
    added_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    room: Mapped["ReadingRoom"] = relationship(back_populates="entries")


class RoomRequest(Base):
    """One member's request to change their OWN room, handled by the concierge
    (ARCH-357 fast-follow). The member types a request; a cheap structured-output
    model returns an action plan; the server re-validates every doc_id against the
    member's live entitlement and applies it, or denies a grant-widening ask and
    logs it for a human to find later (the decision plane it used to escalate to
    is gone, 2026-08-18). This row is the audit record: the raw request body, the
    model's plan, the outcome, and the reply shown."""

    __tablename__ = "room_requests"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # rreq_<ulid>
    room_id: Mapped[str] = mapped_column(
        ForeignKey("reading_rooms.id", ondelete="CASCADE"), index=True)
    actor: Mapped[str] = mapped_column(String(200))               # the member's email
    body: Mapped[str] = mapped_column(Text)                       # the raw request text
    status: Mapped[str] = mapped_column(String(20))              # applied | denied | failed
    plan: Mapped[dict] = mapped_column(JSON, default=dict)        # the model's structured plan (audit)
    reply: Mapped[str] = mapped_column(Text, default="")         # what the member was shown
    # DEAD COLUMN, kept rather than migrated out (2026-08-18): it held the id of
    # a decision this request escalated to, in the plane that has been removed.
    # No code sets it any more; a pre-existing row may still carry one.
    decision_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PushSubscription(Base):
    """A browser's Web Push registration (ARCH-362 Phase 4b).

    The half of the Phase 4 gate that makes an alert REACH the operator rather than wait
    for them. Acknowledgment (4a) used to record that they had seen one; that lived
    on `Signal`/`SignalAck`, both removed with the rest of the vault copy
    (2026-08-19) — push is what tells them there is something to see while they are
    away from a browser, and stands on its own.

    PER SUBSCRIPTION, not per person: one identity has several browsers, each its
    own row, so revoking one device does not silence the others. `endpoint` is
    unique because it is the push service's identifier for this registration —
    re-subscribing the same browser must UPDATE its row, never accumulate
    duplicates that would each deliver the same notification.

    `p256dh` and `auth` are the browser's PUBLIC encryption material, not secrets
    of ours: they let us encrypt a payload only that browser can decrypt. The
    VAPID private key is deliberately absent from this table — it lives in
    ~/.orient-control/vapid.json at 0600, so a database read never yields the
    ability to sign as us.
    """

    __tablename__ = "push_subscriptions"
    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_push_subscriptions_endpoint"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # push_<ulid>
    identity: Mapped[str] = mapped_column(String(200), index=True)
    endpoint: Mapped[str] = mapped_column(Text)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Delivery health: a push service returns 404/410 once a browser discards a
    # subscription. Those are pruned rather than retried forever.
    last_ok_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class AlertPolicy(Base):
    """A dashboard alert threshold, settable from the page whose metric it
    gates (issue_01M17W6XGXVHNDWCFRTQ38Y2RK).

    Orient's own concern (ADR-0022), not the Library's: `code_health_build.
    ci_status`, `dashboard_build.overage_status` and `kb_build.cls_recall` are
    Orient's own evaluators, called from inside this same repo by both a
    source dashboard's own render and `alerts_build.py`'s /alerts feed —
    nothing outside Orient reads or writes a threshold, unlike the deny-list
    epic's policy table, which the Library holds because the Library's own
    clients evaluate it.

    One row per named threshold (`alert_policy_store.DEFAULTS`'s keys), never
    an alert itself: The operator ruled out alerts persisting in the Chart House,
    settling that only the POLICY that triggers one is durable state.

    `values` is a JSON blob rather than fixed columns, because Code Health's
    shape (two rate cutoffs) and Spending's (a run count) are not the same
    shape, and forcing one schema on both would invent structure neither
    value has.

    A missing row means "never set", and every evaluator falls back to
    `alert_policy_store.DEFAULTS[key]` — the value hardcoded here before this
    table existed — so an untouched threshold behaves exactly as it did on
    main.
    """

    __tablename__ = "alert_policies"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    values: Mapped[dict] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)
