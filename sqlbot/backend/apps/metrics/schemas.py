from datetime import date, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MetricCode = Literal['payment_amount', 'paid_orders', 'average_order_value', 'refund_amount']
Dimension = Literal['channel', 'region']
Identifier = Annotated[str, Field(min_length=1, max_length=63, pattern=r'^[\w\u4e00-\u9fff]+$')]
CATALOG = {
    'payment_amount': dict(name='支付金额', unit='元', formula='SUM(支付金额)',
                          description='按支付时间统计成功支付订单的金额，不扣退款。'),
    'paid_orders': dict(name='支付订单数', unit='单', formula='COUNT(支付订单)',
                       description='按支付时间统计成功支付订单，一行对应一个订单。'),
    'average_order_value': dict(name='客单价', unit='元/单', formula='支付金额 / 支付订单数',
                               description='按本指标绑定的支付订单口径计算金额总和 ÷ 订单数；无订单时为空。'),
    'refund_amount': dict(name='退款金额', unit='元', formula='SUM(成功退款金额)',
                         description='按退款成功时间统计，每笔成功退款一行，包含跨期退款。'),
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class MetricBinding(StrictModel):
    metric_code: MetricCode
    table_name: Identifier
    grain_field: Identifier
    amount_field: Identifier | None = None
    time_field: Identifier
    status_field: Identifier
    status_values: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(min_length=1, max_length=20)
    dimensions: dict[Dimension, Identifier] = Field(default_factory=dict)
    timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'

    @model_validator(mode='after')
    def validate_amount(self):
        if self.metric_code == 'paid_orders':
            self.amount_field = None
        if self.metric_code != 'paid_orders' and not self.amount_field:
            raise ValueError('金额类指标必须绑定金额字段')
        self.status_values = list(dict.fromkeys(self.status_values))
        return self


class MetricIntent(StrictModel):
    metric_code: MetricCode
    start_date: date
    end_date: date
    dimension: Dimension | None = None
    filters: dict[Dimension, Annotated[str, Field(min_length=1, max_length=200)]] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_dates(self):
        if self.end_date < self.start_date:
            raise ValueError('结束日期不能早于开始日期')
        if (self.end_date - self.start_date).days >= 366:
            raise ValueError('单次查询最多 366 天')
        if self.end_date == date.max:
            raise ValueError('结束日期超出支持范围')
        return self


def resolve_period(period: str, today: date) -> tuple[date, date]:
    if period == 'last_week':
        end = today - timedelta(days=today.weekday() + 1)
        return end - timedelta(days=6), end
    if period == 'this_week':
        return today - timedelta(days=today.weekday()), today
    if period == 'last_month':
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if period == 'this_month':
        return today.replace(day=1), today
    if period == 'yesterday':
        return today - timedelta(days=1), today - timedelta(days=1)
    if period == 'today':
        return today, today
    raise ValueError('不支持的日期范围')


class PublishDefinition(StrictModel):
    datasource_id: int = Field(gt=0)
    expected_version: int = Field(ge=0)
    binding: MetricBinding


class PreviewRequest(StrictModel):
    datasource_id: int = Field(gt=0)
    intent: MetricIntent
    resolution_id: str | None = Field(default=None, min_length=1, max_length=36)


class ResolveRequest(StrictModel):
    datasource_id: int = Field(gt=0)
    question: str = Field(min_length=1, max_length=2000)

    @field_validator('question')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('请输入问题')
        return value.strip()


class ConfirmRequest(StrictModel):
    confirmed: Literal[True]
