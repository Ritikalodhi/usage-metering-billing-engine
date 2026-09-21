from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PlanCode(str, enum.Enum):
    free = "free"
    pro = "pro"


class UsageType(str, enum.Enum):
    api_call = "api_call"
    tokens = "tokens"


class TokenCategory(str, enum.Enum):
    input = "input"
    cached_input = "cached_input"
    output = "output"
    reasoning = "reasoning"


class SubStatus(str, enum.Enum):
    active = "active"
    trialing = "trialing"
    past_due = "past_due"
    canceled = "canceled"
    unpaid = "unpaid"
    incomplete = "incomplete"


class IdemState(str, enum.Enum):
    in_progress = "in_progress"
    completed = "completed"


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(SmallInteger, Identity(), primary_key=True)
    code: Mapped[PlanCode] = mapped_column(
        Enum(PlanCode, name="plan_code", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        unique=True,
    )
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    api_call_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    token_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stripe_price_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("api_call_limit >= 0", name="ck_plans_api_call_limit"),
        CheckConstraint("token_limit >= 0", name="ck_plans_token_limit"),
    )

    tenants: Mapped[list[Tenant]] = relationship("Tenant", back_populates="plan")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    plan_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("plans.id"), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_tenants_stripe_customer", "stripe_customer_id"),
    )

    plan: Mapped[Plan] = relationship("Plan", back_populates="tenants")
    subscriptions: Mapped[list[Subscription]] = relationship(
        "Subscription", back_populates="tenant", cascade="all, delete-orphan"
    )
    idempotency_keys: Mapped[list[IdempotencyKey]] = relationship(
        "IdempotencyKey", back_populates="tenant", cascade="all, delete-orphan"
    )
    usage_events: Mapped[list[UsageEvent]] = relationship(
        "UsageEvent", back_populates="tenant", cascade="all, delete-orphan"
    )
    usage_rollups: Mapped[list[UsageRollup]] = relationship(
        "UsageRollup", back_populates="tenant", cascade="all, delete-orphan"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[int] = mapped_column(SmallInteger, ForeignKey("plans.id"), nullable=False)
    stripe_subscription_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[SubStatus] = mapped_column(
        Enum(SubStatus, name="sub_status", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index(
            "idx_sub_one_active_per_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=sa.text("status IN ('active', 'trialing', 'past_due')"),
        ),
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="subscriptions")
    plan: Mapped[Plan] = relationship("Plan")


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[IdemState] = mapped_column(
        Enum(IdemState, name="idem_state", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        server_default=sa.text("'in_progress'::idem_state"),
    )
    response_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_idem_tenant_key"),
        Index("idx_idem_created_at", "created_at"),
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="idempotency_keys")
    usage_events: Mapped[list[UsageEvent]] = relationship(
        "UsageEvent", back_populates="idempotency_key"
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("idempotency_keys.id"), nullable=False
    )
    type: Mapped[UsageType] = mapped_column(
        Enum(UsageType, name="usage_type", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    category: Mapped[TokenCategory | None] = mapped_column(
        Enum(TokenCategory, name="token_category", values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    unit_price_micro_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_micro_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_usage_events_quantity"),
        CheckConstraint("unit_price_micro_cents >= 0", name="ck_usage_events_unit_price"),
        CheckConstraint("cost_micro_cents >= 0", name="ck_usage_events_cost"),
        CheckConstraint(
            "(type = 'api_call' AND category IS NULL) OR (type = 'tokens' AND category IS NOT NULL)",
            name="ck_category_matches_type",
        ),
        Index("idx_usage_tenant_time", "tenant_id", sa.text("occurred_at DESC")),
        Index("idx_usage_tenant_type_time", "tenant_id", "type", sa.text("occurred_at DESC")),
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="usage_events")
    idempotency_key: Mapped[IdempotencyKey] = relationship(
        "IdempotencyKey", back_populates="usage_events"
    )


class UsageRollup(Base):
    __tablename__ = "usage_rollups"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    period: Mapped[date] = mapped_column(Date, primary_key=True)
    api_calls_used: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=sa.text("0")
    )
    tokens_used: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=sa.text("0")
    )
    cost_micro_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=sa.text("0")
    )
    by_category: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    tenant: Mapped[Tenant] = relationship("Tenant", back_populates="usage_rollups")


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    stripe_event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobFailure(Base):
    __tablename__ = "job_failures"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    job_name: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    failed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
