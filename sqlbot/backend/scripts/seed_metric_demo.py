"""Create a small, clearly labeled synthetic datasource with a read-only account.

Run with the same environment as SQLBot after applying migrations. Reruns preserve
existing data and definitions. Never touches an existing business datasource.
"""
import json
import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psycopg
import sqlbot_xpack  # Initialize upstream extension before importing datasource CRUD.
from psycopg import sql
from sqlmodel import Session, select

from apps.datasource.models.datasource import CoreDatasource, CoreField, CoreTable
from apps.datasource.utils.utils import aes_encrypt
from apps.metrics.schemas import MetricBinding, PublishDefinition, resolve_period
from apps.metrics.service import latest, publish
from common.core.config import settings
from common.core.db import engine

NAME = '电商指标演示（模拟数据）'
DATABASE = 'ecommerce_metrics_demo'
ROLE = 'sqlbot_metrics_demo_reader'


def publish_demo(session, ds):
    actor = SimpleNamespace(id=1, oid=1, isAdmin=True, weight=1)
    for code in ('payment_amount', 'paid_orders', 'average_order_value', 'refund_amount'):
        refund = code == 'refund_amount'
        binding = MetricBinding(metric_code=code, table_name='refunds' if refund else 'orders',
            grain_field='refund_id' if refund else 'order_id', amount_field='refund_amount' if refund else 'payment_amount',
            time_field='succeeded_at' if refund else 'paid_at', status_field='status',
            status_values=['success'] if refund else ['paid', 'refunded'], dimensions={'channel': 'channel', 'region': 'region'})
        if latest(session, 1, ds.id, code) is None:
            publish(session, actor, PublishDefinition(datasource_id=ds.id, expected_version=0, binding=binding))


def seed():
    with Session(engine) as session:
        ds = session.exec(select(CoreDatasource).where(CoreDatasource.name == NAME, CoreDatasource.oid == 1)).first()
        if ds:
            publish_demo(session, ds)
            print('Demo datasource already exists; data and definitions preserved. ID:', ds.id)
            return
        password = secrets.token_urlsafe(32)
        args = dict(host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT,
                    user=settings.POSTGRES_USER, password=settings.POSTGRES_PASSWORD)
        with psycopg.connect(**args, dbname=settings.POSTGRES_DB, autocommit=True) as conn:
            if conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', (DATABASE,)).fetchone() or \
               conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (ROLE,)).fetchone():
                raise RuntimeError('Demo database/role already exists without registration; refusing to overwrite it.')
            conn.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(ROLE), sql.Literal(password)))
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(DATABASE)))
            conn.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only = on').format(sql.Identifier(ROLE)))
        start, end = resolve_period('last_week', datetime.now(ZoneInfo('Asia/Shanghai')).date())
        midnight = lambda day: datetime.combine(day, datetime.min.time())
        with psycopg.connect(**args, dbname=DATABASE) as conn:
            conn.execute('CREATE TABLE orders (order_id text PRIMARY KEY, payment_amount numeric(16,2) NOT NULL, '
                         'paid_at timestamp NOT NULL, status text NOT NULL, channel text, region text)')
            conn.execute('CREATE TABLE refunds (refund_id text PRIMARY KEY, order_id text NOT NULL, '
                         'refund_amount numeric(16,2) NOT NULL, succeeded_at timestamp NOT NULL, '
                         'status text NOT NULL, channel text, region text)')
            with conn.cursor() as cursor:
                cursor.executemany('INSERT INTO orders VALUES (%s,%s,%s,%s,%s,%s)', [
                    ('o1', 100, midnight(start), 'paid', 'Web', '上海'),
                    ('o2', 201, midnight(end) + timedelta(hours=23, minutes=59, seconds=59), 'refunded', 'App', '北京'),
                    ('o3', 50, midnight(start + timedelta(days=2)), 'paid', 'Web', '上海'),
                    ('o4', 888, midnight(start + timedelta(days=1)), 'unpaid', 'Web', '上海'),
                    ('o5', 999, midnight(end + timedelta(days=1)), 'paid', 'App', '北京'),
                    ('old', 200, midnight(start - timedelta(days=7)), 'refunded', 'Web', '上海'),
                ])
                cursor.executemany('INSERT INTO refunds VALUES (%s,%s,%s,%s,%s,%s,%s)', [
                    ('r1', 'old', 30, midnight(start + timedelta(days=1)), 'success', 'Web', '上海'),
                    ('r2', 'old', 10, midnight(start + timedelta(days=3)), 'success', 'Web', '上海'),
                    ('r3', 'o2', 201, midnight(end), 'pending', 'App', '北京'),
                    ('r4', 'o1', 90, midnight(end + timedelta(days=1)), 'success', 'Web', '上海'),
                ])
            conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(DATABASE), sql.Identifier(ROLE)))
            conn.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(ROLE)))
            conn.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(ROLE)))
            conn.execute('CREATE TABLE demo_reference (start_date date, end_date date, payment_amount numeric, '
                         'paid_orders integer, average_order_value numeric, refund_amount numeric)')
            conn.execute('INSERT INTO demo_reference VALUES (%s,%s,351,3,117,40)', (start, end))
        conf = dict(host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, username=ROLE, password=password,
                    database=DATABASE, dbSchema='public', timeout=30, poolSize=2)
        ds = CoreDatasource(name=NAME, description=f'固定模拟数据；参考日期 {start} 至 {end}。支付金额351元、订单3单、客单价117元、退款40元。',
            type='pg', type_name='PostgreSQL', configuration=aes_encrypt(json.dumps(conf)).decode(),
            create_time=datetime.now(), create_by=1, status='Success', num='2', oid=1, table_relation=[], recommended_config=1)
        session.add(ds)
        session.flush()
        for table_name in ('orders', 'refunds'):
            table = CoreTable(ds_id=ds.id, checked=True, table_name=table_name,
                              table_comment='电商指标模拟数据', custom_comment='每笔订单/退款一行')
            session.add(table)
            session.flush()
            with psycopg.connect(**args, dbname=DATABASE) as conn:
                columns = conn.execute('SELECT column_name,data_type,ordinal_position FROM information_schema.columns '
                                       'WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position',
                                       ('public', table_name)).fetchall()
            for name, kind, index in columns:
                session.add(CoreField(ds_id=ds.id, table_id=table.id, checked=True, field_name=name, field_type=kind,
                    field_comment=name, custom_comment=name, field_index=index))
        session.commit()
        publish_demo(session, ds)
        print(f'Synthetic demo ready. Datasource ID: {ds.id}; dates: {start} to {end}.')
        print('Expected: payment_amount=351; paid_orders=3; average_order_value=117; refund_amount=40.')


if __name__ == '__main__':
    seed()
