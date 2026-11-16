from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Column, JSON, UniqueConstraint, Index, Text
from sqlmodel import Field, SQLModel


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MetricDefinition(SQLModel, table=True):
    __tablename__ = 'metric_definition'
    __table_args__ = (UniqueConstraint('oid', 'datasource_id', 'metric_code', 'version', name='uq_metric_version'),)
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=36)
    oid: int
    datasource_id: int
    metric_code: str = Field(max_length=40)
    version: int
    binding: dict = Field(sa_column=Column(JSON, nullable=False))
    created_by: int
    created_at: datetime = Field(default_factory=now_utc)


class MetricQuery(SQLModel, table=True):
    __tablename__ = 'metric_query'
    __table_args__ = (Index('ix_metric_query_owner', 'oid', 'user_id', 'created_at'),)
    id: str = Field(default_factory=lambda: str(uuid4()), primary_key=True, max_length=36)
    oid: int
    user_id: int
    datasource_id: int
    definition_id: str | None = Field(default=None, max_length=36)
    plan: dict = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default='preview', max_length=20)
    result: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    created_at: datetime = Field(default_factory=now_utc)
    question: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    resolution: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    events: list | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    error: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
