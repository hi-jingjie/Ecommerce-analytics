from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    refunds_csv_text: str | None = Field(default=None, min_length=1, max_length=2_000_000)


class ChannelBill(BaseModel):
    csv_text: str = Field(min_length=1, max_length=2_000_000)

    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode='after')
    def validate_period(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError('账期需要同时填写开始和结束日期')
        if self.start_date is not None and self.start_date > self.end_date:
            raise ValueError('账期开始日期不能晚于结束日期')
        return self
