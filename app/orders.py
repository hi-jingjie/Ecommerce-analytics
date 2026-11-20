"""Parse and validate a complete order snapshot before opening its transaction."""
import csv
import io
from datetime import date
from decimal import Decimal

from fastapi import HTTPException

from .models import Order, RefundEvent


def parse_orders(csv_text: str, refunds_csv_text: str | None = None) -> list[Order]:
    reader = csv.DictReader(io.StringIO(csv_text, newline=''), strict=True)
    required = {'order_id', 'day', 'region', 'channel', 'amount', 'status', 'refund', 'refund_day'}
    try:
        fields = reader.fieldnames or []
    except csv.Error as exc:
        raise HTTPException(422, f'CSV 表头格式错误：{exc}')
    if len(fields) != len(required) or set(fields) != required:
        raise HTTPException(422, 'CSV 列必须为 '+','.join(sorted(required)))
    rows, ids = [], set()
    try:
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError('字段数量与表头不一致')
            if len(rows) >= 10000:
                raise ValueError('最多导入 10000 行')
            if not row['order_id'].strip() or len(row['order_id']) > 128 or row['order_id'] in ids:
                raise ValueError('订单编号必须非空、至多 128 字且在数据源内唯一')
            if any('\x00' in value for value in row.values()):
                raise ValueError('字段含有无效字符')
            ids.add(row['order_id'])
            amount, refund = Decimal(row['amount']), Decimal(row['refund'] or '0')
            if not amount.is_finite() or not refund.is_finite() or not (0 <= refund <= amount < 10**12) or amount != amount.quantize(Decimal('.01')) or refund != refund.quantize(Decimal('.01')):
                raise ValueError('金额必须非负且最多两位小数，退款不超过支付金额')
            if row['status'] not in ('paid', 'cancelled') or not all(0 < len(row[k]) <= 80 for k in ('region', 'channel')):
                raise ValueError('状态、地区或渠道无效')
            day = date.fromisoformat(row['day'])
            refund_day = date.fromisoformat(row['refund_day']) if row['refund_day'] else None
            if refund and (not refund_day or refund_day < day or row['status'] != 'paid'):
                raise ValueError('退款必须提供不早于支付日期的退款日期且订单已支付')
            rows.append(Order(order_id=row['order_id'], day=day, region=row['region'], channel=row['channel'],
                              amount_cents=int(amount * 100), status=row['status'], refund_cents=int(refund * 100),
                              refund_day=refund_day))
    except (ValueError, ArithmeticError, TypeError, csv.Error) as exc:
        raise HTTPException(422, f'CSV 第 {len(rows)+2} 行格式错误：{exc}')
    if not rows:
        raise HTTPException(422, 'CSV 没有数据')
    if refunds_csv_text is not None:
        attach_refund_details(rows, refunds_csv_text)
    for order in rows:
        if order.refund_day is not None:
            order.refund_events.append(RefundEvent(day=order.refund_day, amount_cents=order.refund_cents))
    return rows


def attach_refund_details(orders: list[Order], csv_text: str):
    by_id = {order.order_id: order for order in orders}
    seen = set()
    totals = {}
    reader = csv.DictReader(io.StringIO(csv_text, newline=''), strict=True)
    columns = {'order_id', 'refund_id', 'refund_day', 'refund'}
    try:
        fields = reader.fieldnames or []
        if set(fields) != columns or len(fields) != len(columns):
            raise ValueError('表头应包含 order_id,refund_id,refund_day,refund')
        for line, row in enumerate(reader, start=1):
            if line > 10000 or None in row or any(v is None or '\x00' in v for v in row.values()):
                raise ValueError('行数超限或字段格式错误')
            oid, rid = row['order_id'], row['refund_id']
            order = by_id.get(oid)
            if order is None or order.status != 'paid':
                raise ValueError('退款必须关联本次导入的已支付订单')
            if order.refund_cents or order.refund_day is not None:
                raise ValueError('使用明细的订单应将订单行退款填 0、退款日期留空')
            if not rid.strip() or len(rid) > 128 or (oid, rid) in seen:
                raise ValueError('退款编号须为 1 至 128 字且在该订单内唯一')
            day = date.fromisoformat(row['refund_day'])
            amount = Decimal(row['refund'])
            if (day < order.day or not amount.is_finite() or not 0 < amount < 10**12
                    or amount != amount.quantize(Decimal('.01'))):
                raise ValueError('退款日期不能早于支付日，金额必须为正且最多两位小数')
            cents = int(amount * 100)
            totals[oid] = totals.get(oid, 0) + cents
            if totals[oid] > order.amount_cents:
                raise ValueError('该订单累计退款超过支付金额')
            seen.add((oid, rid))
            order.refund_events.append(RefundEvent(refund_id=rid, day=day, amount_cents=cents))
    except (ValueError, ArithmeticError, csv.Error) as exc:
        raise HTTPException(422, f'退款明细第 {reader.line_num} 行格式错误：{exc}') from exc

