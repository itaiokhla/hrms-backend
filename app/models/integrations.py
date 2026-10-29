"""Channels and feeds (AS-M12 / G-24; migration 0058).

* ``notification_channels`` — tenant connections: Slack and Microsoft Teams incoming webhooks, signed generic
  webhooks (HMAC-SHA256 ``X-HRMS-Signature``) and a WhatsApp provider slot. URL and secret are encrypted with the
  tenant data key; ``events`` lists the subscribed event types.
* ``channel_deliveries`` — one row per (channel, event): QUEUED -> SENT / FAILED with attempts and the last error; a
  job delivers them so a slow endpoint never blocks a user request.
* ``push_subscriptions`` — browser push endpoints of a user (Web Push with VAPID; the payload-less "tickle" makes
  the service worker fetch the newest notification).
* ``calendar_feed_tokens`` — a secret per user for the read-only ICS feed of their published schedule.
"""

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.models.base import BaseModel, TenantMixin

CHANNEL_KINDS = ("SLACK", "TEAMS", "WEBHOOK", "WHATSAPP")
DELIVERY_STATUSES = ("QUEUED", "SENT", "FAILED", "SKIPPED")


def _fk(table: str, column: str, target: str, name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["company_id", column], [f"{target}.company_id", f"{target}.id"],
                                name=f"fk_{table}_{name}")


def _in(values) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


class NotificationChannel(TenantMixin, BaseModel):
    __tablename__ = "notification_channels"

    kind = Column(String(10), nullable=False)
    name = Column(String(100), nullable=False)
    url_enc = Column(Text, nullable=True)
    url_hint = Column(String(80), nullable=True)
    secret_enc = Column(Text, nullable=True)
    events = Column(JSONB, nullable=False, default=list)
    active = Column(Boolean, nullable=False, default=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(300), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    row_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_notification_channels_company_id_id"),
        CheckConstraint(f"kind IN {_in(CHANNEL_KINDS)}", name="ck_notification_channels_kind"),
        CheckConstraint("jsonb_typeof(events) = 'array'", name="ck_notification_channels_events"),
    )


class ChannelDelivery(TenantMixin, BaseModel):
    __tablename__ = "channel_deliveries"

    channel_id = Column(Integer, nullable=False, index=True)
    event_type = Column(String(60), nullable=False)
    event_key = Column(String(120), nullable=False)
    payload = Column(JSONB, nullable=False, default=dict)
    status = Column(String(8), nullable=False, default="QUEUED")
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(300), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_channel_deliveries_company_id_id"),
        UniqueConstraint("company_id", "channel_id", "event_key", name="uq_channel_deliveries_event"),
        _fk("channel_deliveries", "channel_id", "notification_channels", "channel"),
        CheckConstraint(f"status IN {_in(DELIVERY_STATUSES)}", name="ck_channel_deliveries_status"),
        Index("ix_channel_deliveries_queued", "company_id", "status"),
    )


class PushSubscription(TenantMixin, BaseModel):
    __tablename__ = "push_subscriptions"

    user_id = Column(Integer, nullable=False, index=True)
    endpoint = Column(Text, nullable=False)
    p256dh = Column(String(200), nullable=True)
    auth = Column(String(100), nullable=True)
    user_agent = Column(String(200), nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(String(300), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_push_subscriptions_company_id_id"),
        UniqueConstraint("company_id", "user_id", "endpoint", name="uq_push_subscriptions_endpoint"),
        _fk("push_subscriptions", "user_id", "users", "user"),
    )


class CalendarFeedToken(TenantMixin, BaseModel):
    __tablename__ = "calendar_feed_tokens"

    user_id = Column(Integer, nullable=False)
    employee_id = Column(Integer, nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    active = Column(Boolean, nullable=False, default=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_calendar_feed_tokens_company_id_id"),
        _fk("calendar_feed_tokens", "user_id", "users", "user"),
        _fk("calendar_feed_tokens", "employee_id", "employees", "employee"),
    )
