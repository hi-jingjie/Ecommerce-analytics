"""Parse and validate a complete order snapshot before opening its transaction."""
import csv
import io
from datetime import date
from decimal import Decimal

from fastapi import HTTPException

from .models import Order


def parse_orders(csv_text: str) -> list[Order]:
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
    return rows
