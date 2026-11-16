"""Contract tests: executing generated SQL must preserve ecommerce semantics."""
import importlib.util
import sqlite3
from datetime import date

import pytest


def test_metrics_feature_exists():
    assert importlib.util.find_spec('apps.metrics') is not None, 'metric module is missing'


def components():
    from apps.metrics.schemas import MetricBinding, MetricIntent
    from apps.metrics.compiler import compile_metric
    return MetricBinding, MetricIntent, compile_metric


def binding(code='payment_amount', **updates):
    Binding, _, _ = components()
    values = dict(metric_code=code, table_name='orders', grain_field='id',
                  amount_field='amount', time_field='paid_at', status_field='status',
                  status_values=['paid', 'refunded'], dimensions={'channel': 'channel'})
    values.update(updates)
    return Binding(**values)


def intent(code='payment_amount', **updates):
    _, Intent, _ = components()
    values = dict(metric_code=code, start_date='2026-10-19', end_date='2026-10-25')
    values.update(updates)
    return Intent(**values)


@pytest.fixture
def db():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE orders(id TEXT, amount NUMERIC, paid_at TEXT, status TEXT, channel TEXT)')
    connection.executemany('INSERT INTO orders VALUES(?,?,?,?,?)', [
        ('1', 100, '2026-10-19 00:00:00', 'paid', 'Web'),
        ('2', 201, '2026-10-25 23:59:59', 'refunded', 'App'),
        ('3', 999, '2026-10-26 00:00:00', 'paid', 'Web'),
        ('4', 888, '2026-10-20 10:00:00', 'unpaid', 'Web'),
    ])
    yield connection
    connection.close()


def run(db, code='payment_amount', **kwargs):
    _, _, compile_metric = components()
    query = compile_metric(binding(code), intent(code, **kwargs), schema='main')
    return [dict(row) for row in db.execute(query.sql, query.params)]


@pytest.mark.parametrize('code, expected', [('payment_amount', 301), ('paid_orders', 2), ('average_order_value', 150.5)])
def test_metrics_use_paid_order_grain_and_inclusive_end_day(db, code, expected):
    assert run(db, code) == [{'value': expected}]


def test_group_and_filter_are_applied_without_interpolating_values(db):
    assert run(db, dimension='channel') == [{'dimension': 'App', 'value': 201}, {'dimension': 'Web', 'value': 100}]
    assert run(db, filters={'channel': "Web' OR 1=1 --"}) == [{'value': 0}]


def test_empty_average_is_null_and_amount_is_zero(db):
    assert run(db, 'average_order_value', start_date='2025-01-01', end_date='2025-01-01') == [{'value': None}]
    assert run(db, start_date='2025-01-01', end_date='2025-01-01') == [{'value': 0}]


def test_duplicate_or_null_grain_is_detectable_before_aggregation(db):
    _, _, compile_metric = components()
    db.execute("INSERT INTO orders VALUES('1', 100, '2026-10-20 00:00:00','paid','Web')")
    query = compile_metric(binding(), intent(), schema='main')
    row = dict(db.execute(query.integrity_sql, query.params).fetchone())
    assert row['row_count'] == 3 and row['grain_count'] == 2


def test_refunds_use_refund_time_and_success_status(db):
    _, _, compile_metric = components()
    db.execute('CREATE TABLE refunds(id TEXT, amount NUMERIC, succeeded_at TEXT, status TEXT)')
    db.executemany('INSERT INTO refunds VALUES(?,?,?,?)', [
        ('r1', 30, '2026-10-20 00:00:00', 'success'),
        ('r2', 10, '2026-10-22 00:00:00', 'success'),
        ('r3', 50, '2026-10-20 00:00:00', 'pending'),
        ('r4', 90, '2026-10-26 00:00:00', 'success'),
    ])
    query = compile_metric(binding('refund_amount', table_name='refunds', time_field='succeeded_at',
                                   status_values=['success'], dimensions={}), intent('refund_amount'), schema='main')
    assert dict(db.execute(query.sql, query.params).fetchone()) == {'value': 40}


def test_permission_filter_is_applied_before_aggregation(db):
    _, _, compile_metric = components()
    query = compile_metric(binding(), intent(), schema='main', row_filter='"channel" = \'Web\'')
    assert dict(db.execute(query.sql, query.params).fetchone()) == {'value': 100}


@pytest.mark.parametrize('updates', [dict(end_date='2026-10-01'), dict(start_date='2020-01-01'),
    dict(metric_code='arbitrary_sql'), dict(dimension='product'), dict(filters={'sql': 'DROP TABLE orders'}),
    dict(sql='SELECT 1')])
def test_rejects_invalid_or_unbounded_intent(updates):
    with pytest.raises(ValueError):
        intent(**updates)


def test_identifiers_are_quoted_and_cannot_inject_statements(db):
    _, _, compile_metric = components()
    with pytest.raises(ValueError):
        binding(table_name='orders; DROP TABLE orders')
    with pytest.raises(ValueError):
        compile_metric(binding(), intent(dimension='region'), schema='main')


def test_last_week_resolves_in_business_calendar():
    from apps.metrics.schemas import resolve_period
    assert resolve_period('last_week', date(2026, 10, 29)) == (date(2026, 10, 19), date(2026, 10, 25))


def test_order_count_never_reads_optional_amount_column(db):
    _, _, compile_metric = components()
    query = compile_metric(binding('paid_orders', amount_field='restricted_amount'), intent('paid_orders'), schema='main')
    assert dict(db.execute(query.integrity_sql, query.params).fetchone())['null_amount_count'] == 0
    assert dict(db.execute(query.sql, query.params).fetchone()) == {'value': 2}
