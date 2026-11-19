"""Fixed metric definitions shared by previews and history."""
from decimal import Decimal

from sqlalchemy import func, select

from .models import Order


def aggregate(db, data, user):
    day = Order.refund_day if data.metric == 'refund_amount' else Order.day
    cents = Order.refund_cents if data.metric == 'refund_amount' else Order.amount_cents
    stmt = select(func.count(Order.id), func.coalesce(func.sum(cents), 0)).where(
        day >= data.start_date, day <= data.end_date, Order.status == 'paid',
        Order.source_id == data.source_id,
    )
    if user['region'] is not None:
        stmt = stmt.where(Order.region == user['region'])
    if data.channel:
        stmt = stmt.where(Order.channel == data.channel)
    count, total_cents = db.execute(stmt).one()
    value = Decimal(total_cents).scaleb(-2)
    if data.metric == 'paid_orders':
        value = Decimal(count)
    elif data.metric == 'average_order_value' and count:
        value /= count
    return value.quantize(Decimal('.01')), count
