from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey,
                        Index, Integer, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass

class Bootstrap(Base):
    __tablename__ = 'bootstrap'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)
    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Source(Base):
    __tablename__ = 'sources'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    version: Mapped[int] = mapped_column(default=1)

class Grant(Base):
    __tablename__ = 'grants'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('sources.id'), primary_key=True)

class Preview(Base):
    __tablename__ = 'previews'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    payload: Mapped[str] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_version: Mapped[int] = mapped_column(Integer)
    expires: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)

class Token(Base):
    __tablename__ = 'sessions'
    id: Mapped[int] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    expires: Mapped[datetime] = mapped_column(DateTime)

class SchemaVersion(Base):
    __tablename__ = 'schema_version'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    version: Mapped[int] = mapped_column(Integer)


# This covering index was measured against PostgreSQL refund aggregates.
refund_scope_index = Index('ix_orders_source_region_status_id', 'source_id', 'region', 'status', 'id').ddl_if(
    dialect='postgresql'
)


class Order(Base):
    __tablename__ = 'orders'
    __table_args__ = (
        UniqueConstraint('source_id', 'order_id', name='uq_orders_source_order'),
        refund_scope_index,
        CheckConstraint('length(trim(order_id)) > 0 AND length(order_id) <= 128', name='ck_orders_order_id'),
        CheckConstraint('amount_cents BETWEEN 0 AND 99999999999999', name='ck_orders_amount'),
        CheckConstraint('refund_cents BETWEEN 0 AND amount_cents', name='ck_orders_refund'),
        CheckConstraint("typeof(amount_cents) = 'integer' AND typeof(refund_cents) = 'integer'",
                        name='ck_orders_integer_cents').ddl_if(dialect='sqlite'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('sources.id'), index=True)
    order_id: Mapped[str] = mapped_column(String(128))
    order_id_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    day: Mapped[date] = mapped_column(Date)
    region: Mapped[str] = mapped_column(String(80))
    channel: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    refund_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    refund_day: Mapped[date | None] = mapped_column(Date, nullable=True)

    refund_events: Mapped[list['RefundEvent']] = relationship()


class RefundEvent(Base):
    __tablename__ = 'refund_events'
    __table_args__ = (
        UniqueConstraint('order_pk', 'refund_id', name='uq_order_refund_id'),
        Index('ix_refund_events_day_order', 'day', 'order_pk'),
        CheckConstraint('amount_cents BETWEEN 0 AND 99999999999999', name='ck_refund_event_amount'),
        CheckConstraint("typeof(amount_cents) = 'integer'", name='ck_refund_event_integer').ddl_if(dialect='sqlite'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_pk: Mapped[int] = mapped_column(ForeignKey('orders.id'))
    refund_id: Mapped[str | None] = mapped_column(String(128))
    day: Mapped[date] = mapped_column(Date)
    amount_cents: Mapped[int] = mapped_column(BigInteger)


class History(Base):
    __tablename__ = 'query_history'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel: Mapped[str | None] = mapped_column(String(80), nullable=True)
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    metric: Mapped[str] = mapped_column(String(40))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    __table_args__ = (
        Index('ix_query_history_user_id_id', 'user_id', 'id'),
        CheckConstraint('value_cents >= 0', name='ck_history_value'),
        CheckConstraint("typeof(value_cents) = 'integer'",
                        name='ck_history_integer_cents').ddl_if(dialect='sqlite'),
    )
    source_name: Mapped[str | None] = mapped_column(String(80))
    source_version: Mapped[int | None] = mapped_column(Integer)
    definition_version: Mapped[int | None] = mapped_column(Integer)
    preview_id: Mapped[str | None] = mapped_column(String(64))
    matched_count: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    value_cents: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def value(self) -> Decimal:
        return Decimal(self.value_cents).scaleb(-2)

    @value.setter
    def value(self, value: Decimal):
        cents = Decimal(value).scaleb(2)
        if cents != cents.to_integral_value():
            raise ValueError('History values must be whole cents')
        self.value_cents = int(cents)


class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(48))
    outcome: Mapped[str] = mapped_column(String(16))
    status_code: Mapped[int] = mapped_column(Integer)
    details: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Reconciliation(Base):
    __tablename__ = 'refund_reconciliations'
    __table_args__ = (
        UniqueConstraint('source_id', 'bill_digest', name='uq_source_channel_bill'),
        Index('ix_reconciliation_source_created_id', 'source_id', 'created_at', 'id'),
    )
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('sources.id'))
    bill_digest: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    report: Mapped[str] = mapped_column(Text)
