"""Compare one immutable snapshot with a complete channel refund statement."""
import csv
import hashlib
import io
import json
from datetime import date, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import Order, Reconciliation, RefundEvent


def parse_bill(contents, start_date=None, end_date=None):
    reader = csv.DictReader(io.StringIO(contents.removeprefix('\ufeff'), newline=''), strict=True)
    amounts = {}
    try:
        if len(contents.encode("utf-8")) > 2_000_000:
            raise ValueError("渠道账单最多 2 MB")
        fields = reader.fieldnames or []
        required = {'order_id', 'refund_id', 'refund'}
        if len(fields) != len(set(fields)) or set(fields) not in (required, required | {'refund_day'}):
            raise ValueError('必需列为 order_id,refund_id,refund，可选列为 refund_day')
        for row in reader:
            if len(amounts) >= 20000 or None in row or None in row.values():
                raise ValueError('行数超限或字段数错误')
            if 'refund_day' in row:
                day = date.fromisoformat(row['refund_day'])
                if start_date is not None and not start_date <= day <= end_date:
                    raise ValueError('退款日期超出所选账期')
            key = (row['order_id'], row['refund_id'])
            if any(not v.strip() or len(v) > 128 or '\x00' in v for v in key):
                raise ValueError('订单号和退款编号须为 1 至 128 字')
            if key in amounts:
                raise ValueError('同一订单的退款编号重复')
            value = Decimal(row['refund'])
            if not value.is_finite() or not 0 < value < 10**12 or value != value.quantize(Decimal('.01')):
                raise ValueError('退款金额须为正数、小于一万亿元且精确到分')
            amounts[key] = int(value * 100)
    except (ValueError, ArithmeticError, csv.Error) as exc:
        raise HTTPException(422, f'渠道账单第 {reader.line_num} 行格式错误：{exc}') from exc
    return amounts


def money(cents):
    return format(Decimal(cents).scaleb(-2), '.2f') if cents is not None else None


def compare(db, source_id, channel, start_date, end_date):
    system = {}
    legacy = set()
    stmt = (select(Order.order_id, RefundEvent.refund_id, RefundEvent.amount_cents)
            .join(RefundEvent, RefundEvent.order_pk == Order.id)
            .where(Order.source_id == source_id, Order.status == 'paid', RefundEvent.amount_cents > 0))
    if start_date is not None:
        stmt = stmt.where(RefundEvent.day.between(start_date, end_date))
    for oid, rid, cents in db.execute(stmt):
        system[oid, rid] = cents
        if rid is None:
            legacy.add(oid)

    items, matched = [], 0
    for oid, rid in sorted(system.keys() | channel.keys(), key=lambda key: (key[0], key[1] or '')):
        left, right = system.get((oid, rid)), channel.get((oid, rid))
        delta = (left or 0) - (right or 0)
        if rid is None or (left is None and oid in legacy):
            status = 'review'
            delta = None
        elif left is None:
            status = 'channel_only'
        elif right is None:
            status = 'system_only'
        elif delta:
            status = 'amount_mismatch'
        else:
            matched += 1
            continue
        items.append(dict(order_id=oid, refund_id=rid, status=status, system_amount=money(left),
                          channel_amount=money(right), difference=money(delta)))
    left_total, right_total = sum(system.values()), sum(channel.values())
    return dict(system_total=money(left_total), channel_total=money(right_total),
                difference=money(left_total-right_total), matched=matched, items=items)


def present(record):
    return dict(id=record.id, source_id=record.source_id, created_by=record.created_by,
                start_date=record.start_date, end_date=record.end_date,
                created_at=record.created_at.replace(tzinfo=timezone.utc), **json.loads(record.report))


def save(db, source_id, actor_id, channel, start_date, end_date):
    normalized = [[oid, rid, cents] for (oid, rid), cents in sorted(channel.items())]
    # Keep v4 full-snapshot digests byte-for-byte compatible.
    if start_date is not None:
        normalized = [start_date.isoformat(), end_date.isoformat(), normalized]
    digest = hashlib.sha256(json.dumps(normalized, separators=(',', ':')).encode()).hexdigest()
    lookup = select(Reconciliation).where(Reconciliation.source_id == source_id, Reconciliation.bill_digest == digest)
    record = db.scalar(lookup)
    if record is not None:
        return record, True
    report = compare(db, source_id, channel, start_date, end_date)
    insert = sqlite_insert if db.get_bind().dialect.name == 'sqlite' else pg_insert
    stmt = insert(Reconciliation).values(source_id=source_id, created_by=actor_id, bill_digest=digest,
                                         start_date=start_date, end_date=end_date,
                                         report=json.dumps(report, ensure_ascii=False, separators=(',', ':')))
    stmt = stmt.on_conflict_do_nothing(index_elements=['source_id', 'bill_digest']).returning(Reconciliation.id)
    inserted_id = db.scalar(stmt)
    return db.scalar(lookup), inserted_id is None

def history_page(db, source_id, limit, before_id):
    reports = Reconciliation
    columns = [reports.id, reports.source_id, reports.created_at, reports.start_date, reports.end_date]
    stmt = select(*columns).where(reports.source_id == source_id)
    if before_id is not None:
        cursor_time = db.scalar(select(reports.created_at).where(reports.id == before_id,
                                                                  reports.source_id == source_id))
        if cursor_time is None:
            raise HTTPException(422, '无效的历史报告游标')
        stmt = stmt.where(tuple_(reports.created_at, reports.id) < (cursor_time, before_id))
    rows = db.execute(stmt.order_by(reports.created_at.desc(), reports.id.desc()).limit(limit + 1)).all()
    items = [{**row._mapping, 'created_at': row.created_at.replace(tzinfo=timezone.utc)} for row in rows[:limit]]
    return {'items': items, 'next_cursor': rows[limit - 1].id if len(rows) > limit else None}
