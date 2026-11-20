"""Measure the application's refund aggregate in the private postgres_smoke cluster."""
import json
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.metrics import aggregate
from app.models import Base
from app.schemas import Query


INDEX = 'ix_orders_source_region_status_id'
SNAPSHOTS = 16
ORDERS = 8000
TARGET = 8
REGION = 'region-14'
FIRST_DAY = date(2026, 1, 1)
WINDOWS = {'one_day': (45, 45), 'quarter': (0, 89)}


def seed(conn):
    conn.exec_driver_sql('''
        INSERT INTO sources (id, name, version)
        SELECT s, 'snapshot-' || s, 1 FROM generate_series(1, %s) s
    ''', (SNAPSHOTS,))
    conn.exec_driver_sql('''
        INSERT INTO orders (id, source_id, order_id, order_id_synthetic, day, region, channel,
                            status, amount_cents, refund_cents)
        SELECT (s-1)*%s+n, s, 'order-' || n, false, date '2026-01-01', 'region-' || (n %% 31),
               'web', CASE WHEN n %% 11 = 0 THEN 'cancelled' ELSE 'paid' END, 10000, 0
        FROM generate_series(1, %s) s CROSS JOIN generate_series(1, %s) n ORDER BY s,n
    ''', (ORDERS, SNAPSHOTS, ORDERS))
    conn.exec_driver_sql('''
        INSERT INTO refund_events (order_pk, refund_id, day, amount_cents)
        SELECT (s-1)*%s+n, 'refund-' || k, date '2026-01-01' + (n %% 89) + k-1,
               100*k+s
        FROM generate_series(1, %s) s CROSS JOIN generate_series(1, %s) n
        CROSS JOIN generate_series(1, 2) k
        WHERE n %% 11 <> 0 AND n %% 3 = 0 AND (k = 1 OR n %% 5 = 0) ORDER BY s,n,k
    ''', (ORDERS, SNAPSHOTS, ORDERS))


def expected(first, last):
    order_ids, cents = set(), 0
    for n in range(1, ORDERS + 1):
        if n % 11 == 0 or n % 3 or f'region-{n % 31}' != REGION:
            continue
        for k in range(1, 3 if n % 5 == 0 else 2):
            if first <= n % 89 + k - 1 <= last:
                order_ids.add(n)
                cents += 100*k + TARGET
    return Decimal(cents).scaleb(-2), len(order_ids)


def sample(conn, label, first, last):
    query = Query(source_id=TARGET, metric='refund_amount', start_date=FIRST_DAY + timedelta(days=first),
                  end_date=FIRST_DAY + timedelta(days=last))
    # Capture the application statement and its bound values before the driver executes it.
    statements = []

    def capture(conn, cursor, sql, params, context, executemany):
        statements.append((sql, params))

    event.listen(conn, 'before_cursor_execute', capture)
    try:
        with Session(bind=conn) as db:
            result = aggregate(db, query, {'region': REGION})
    finally:
        event.remove(conn, 'before_cursor_execute', capture)
    if result != expected(first, last):
        raise AssertionError(f'{label} result {result!r} differs from fixture')
    [(sql, params)] = statements
    print(label, query.start_date, query.end_date, 'result:', result)
    print(sql, params)
    plans = [conn.exec_driver_sql('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql, params).scalar_one()[0]
             for _ in range(8)]
    # Discard one warm-up and retain the median execution with its own buffer counters.
    median_plan = sorted(plans[1:], key=lambda p: p['Execution Time'])[3]
    print(json.dumps(median_plan, indent=2))
    root = median_plan['Plan']
    print(f"SUMMARY {label} orders={result[1]} amount={result[0]} "
          f"median_ms={median_plan['Execution Time']} shared_hit={root['Shared Hit Blocks']} "
          f"shared_read={root['Shared Read Blocks']}")
    return result


def run(cluster_url):
    # The caller supplies its just-created private cluster, and CREATE DATABASE requires a fresh name.
    admin = create_engine(cluster_url, isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        conn.exec_driver_sql('CREATE DATABASE refund_benchmark')
    bench = create_engine(admin.url.set(database='refund_benchmark'), isolation_level='AUTOCOMMIT')
    admin.dispose()
    try:
        with bench.connect() as conn:
            print('PostgreSQL', conn.exec_driver_sql('SHOW server_version').scalar_one())
            Base.metadata.create_all(conn)
            conn.exec_driver_sql(f'DROP INDEX IF EXISTS {INDEX}')
            seed(conn)
            conn.exec_driver_sql('SET default_statistics_target = 1000')
            for table in ('orders', 'refund_events'):
                conn.exec_driver_sql(f'VACUUM (ANALYZE) {table}')
                print(table, conn.exec_driver_sql(f'SELECT count(*) FROM {table}').scalar_one())
            results = {}
            for phase in ('baseline', 'candidate'):
                if phase == 'candidate':
                    conn.exec_driver_sql(f'CREATE INDEX {INDEX} ON orders (source_id, region, status, id)')
                    print('Index bytes:', conn.exec_driver_sql(f"SELECT pg_relation_size('{INDEX}')").scalar_one())
                results[phase] = {name: sample(conn, f'{phase}/{name}', first, last)
                                  for name, (first, last) in WINDOWS.items()}
            if results['baseline'] != results['candidate']:
                raise AssertionError('Results changed after index creation')
    finally:
        bench.dispose()
