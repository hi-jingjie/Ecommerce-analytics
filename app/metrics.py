"""Fixed metric definitions shared by previews and history."""
from decimal import Decimal

from sqlalchemy import func, select

from .models import Order, RefundEvent


def aggregate(db, data, user):
    if data.metric == 'refund_amount':
        stmt = select(func.count(Order.id.distinct()), func.coalesce(func.sum(RefundEvent.amount_cents), 0))
        stmt = stmt.select_from(Order).join(RefundEvent, RefundEvent.order_pk == Order.id)
        day = RefundEvent.day
    else:
        stmt = select(func.count(Order.id), func.coalesce(func.sum(Order.amount_cents), 0))
        day = Order.day
    stmt = stmt.where(day.between(data.start_date, data.end_date), Order.status == 'paid',
                      Order.source_id == data.source_id)
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
