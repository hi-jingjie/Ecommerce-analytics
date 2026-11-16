"""Opt-in integration against configured local PG; creates/drops only its own schema.

Run with SQLBot's environment and METRICS_PG_TEST=1. No production rows are changed.
"""
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from apps.metrics.compiler import CompiledQuery, compile_metric, quote_identifier
from apps.metrics.schemas import MetricBinding, MetricIntent
from apps.metrics.service import run_query

pytestmark = pytest.mark.skipif(os.environ.get('METRICS_PG_TEST') != '1', reason='requires opt-in local PostgreSQL')


@pytest.fixture
def pg():
    import sqlbot_xpack
    from common.core.db import engine
    from common.core.config import settings
    from apps.datasource.models.datasource import CoreDatasource
    from apps.datasource.utils.utils import aes_encrypt
    schema = 'metric_test_' + uuid4().hex
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA {quote_identifier(schema)}'))
        conn.execute(text(f'CREATE TABLE "{schema}".orders (id text, amount numeric, paid_at timestamptz, status text, channel text)'))
        conn.execute(text(f'INSERT INTO "{schema}".orders VALUES '
            "('a',100,'2026-10-18 16:00:00+00','paid','Web'),"
            "('b',201,'2026-10-25 15:59:59+00','paid','App'),"
            "('outside',999,'2026-10-25 16:00:00+00','paid','Web')"))
    conf = dict(host=settings.POSTGRES_SERVER, port=settings.POSTGRES_PORT, username=settings.POSTGRES_USER,
                password=settings.POSTGRES_PASSWORD, database=settings.POSTGRES_DB, dbSchema=schema, timeout=30)
    ds = CoreDatasource(id=-1, type='pg', configuration=aes_encrypt(json.dumps(conf)).decode())
    binding = MetricBinding(metric_code='payment_amount', table_name='orders', grain_field='id',
        amount_field='amount', time_field='paid_at', status_field='status', status_values=['paid'], dimensions={'channel': 'channel'})
    try:
        yield SimpleNamespace(engine=engine, schema=schema, ds=ds, binding=binding)
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {quote_identifier(schema)} CASCADE'))


def query(pg, code='payment_amount', **updates):
    binding = MetricBinding.model_validate({**pg.binding.model_dump(), 'metric_code': code})
    intent = MetricIntent(metric_code=code, start_date='2026-10-19', end_date='2026-10-25', **updates)
    return binding, compile_metric(binding, intent, pg.schema)


def test_postgres_timezone_and_exact_decimal(pg):
    binding, compiled = query(pg)
    assert run_query(pg.ds, binding, compiled) == [{'value': '301'}]
    binding, compiled = query(pg, 'average_order_value')
    from decimal import Decimal
    assert Decimal(run_query(pg.ds, binding, compiled)[0]['value']) == Decimal('150.5')


def test_duplicate_grain_fails_in_real_execution(pg):
    with pg.engine.begin() as conn:
        conn.execute(text(f'INSERT INTO "{pg.schema}".orders VALUES (\'a\',100,\'2026-10-20\',\'paid\',\'Web\')'))
    binding, compiled = query(pg)
    with pytest.raises(HTTPException) as error:
        run_query(pg.ds, binding, compiled)
    assert error.value.status_code == 422
    assert '重复' in error.value.detail


def test_null_amount_does_not_block_count_but_blocks_money(pg):
    with pg.engine.begin() as conn:
        conn.execute(text(f'UPDATE "{pg.schema}".orders SET amount=NULL WHERE id=\'a\''))
    binding, compiled = query(pg, 'paid_orders')
    assert run_query(pg.ds, binding, compiled) == [{'value': 2}]
    binding, compiled = query(pg)
    with pytest.raises(HTTPException) as error:
        run_query(pg.ds, binding, compiled)
    assert '空金额' in error.value.detail


def test_database_transaction_is_read_only(pg):
    binding, compiled = query(pg)
    malicious = CompiledQuery(sql=f'DELETE FROM "{pg.schema}".orders RETURNING amount AS value',
                              params=compiled.params, integrity_sql=compiled.integrity_sql)
    with pytest.raises(HTTPException):
        run_query(pg.ds, binding, malicious)
    with pg.engine.connect() as conn:
        assert conn.execute(text(f'SELECT count(*) FROM "{pg.schema}".orders')).scalar() == 3


def test_group_overflow_fails_instead_of_truncating(pg):
    with pg.engine.begin() as conn:
        conn.execute(text(f'INSERT INTO "{pg.schema}".orders '
                          "SELECT 'id'||n, 1, '2026-10-20'::timestamptz, 'paid', 'channel'||n FROM generate_series(1,1001) AS n"))
    binding, compiled = query(pg, dimension='channel')
    with pytest.raises(HTTPException) as error:
        run_query(pg.ds, binding, compiled)
    assert '1000' in error.value.detail
