from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=128)


class Query(BaseModel):
    source_id: int = Field(default=1, ge=1)
    metric: Literal['payment_amount', 'paid_orders', 'average_order_value', 'refund_amount']
    start_date: date
    end_date: date
    channel: str | None = Field(default=None, max_length=80)


class Access(BaseModel):
    region: str | None = Field(default=None, min_length=1, max_length=80)
    source_ids: list[int] = Field(default_factory=list, max_length=100)
    active: bool = True


class NewUser(Credentials, Access):
    pass


class ImportCSV(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r'^[^\x00]+$')
    csv_text: str = Field(min_length=1, max_length=2_000_000)
