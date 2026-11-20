from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app import main

ORDERS = ('order_id,day,region,channel,amount,status,refund,refund_day\n'
          '001,2026-01-01,east,web,50.00,paid,0,\n'
          '002,2026-01-01,west,app,80.00,paid,0,\n'
          '003,2026-01-01,east,web,20.00,cancelled,0,\n'
          '004,2026-01-01,east,web,20.00,paid,5,2026-02-01\n')
HEADER = 'order_id,refund_id,refund_day,refund\n'


@pytest.fixture
def admin(isolated_engine):
    main.init_db()
    with main.SessionLocal.begin() as db:
        owner = main.User(username='admin', password_hash='unused', admin=True)
        db.add(owner)
        db.flush()
        return {'admin': True, 'region': None, 'id': owner.id}


def test_cross_month_refunds(admin):
    details = HEADER + ('001,A,2026-01-31,10.01\n001,B,2026-02-01,19.99\n'
                        '001,C,2026-02-01,20.00\n002,D,2026-02-01,7.00\n')
    sid = main.import_csv(main.ImportCSV(name='orders', csv_text=ORDERS, refunds_csv_text=details), admin)['id']
    for metric, start, end, channel, value, count in [
        ('refund_amount', '2026-01-31', '2026-01-31', None, '10.01', 1),
        ('refund_amount', '2026-02-01', '2026-02-01', 'web', '44.99', 2),
        ('refund_amount', '2026-01-01', '2026-02-01', None, '62.00', 3),
        ('payment_amount', '2026-01-01', '2026-02-01', None, '150.00', 3),
    ]:
        q = main.Query(metric=metric, source_id=sid, start_date=start, end_date=end, channel=channel)
        p = main.preview(q, admin)
        result = main.execute_preview(p['id'], admin)
        assert (result['value'], result['count']) == (value, count)

    with main.SessionLocal.begin() as db:
        viewer = main.User(username='regional', password_hash='unused', region='west')
        db.add(viewer)
        db.flush()
        db.add(main.Grant(user_id=viewer.id, source_id=sid))
        identity = {'id': viewer.id, 'admin': False, 'region': viewer.region}
    query = main.Query(metric='refund_amount', source_id=sid, start_date='2026-01-01', end_date='2026-02-01')
    result = main.execute_preview(main.preview(query, identity)['id'], identity)
    assert (result['value'], result['count']) == ('7.00', 1)


def test_invalid_refund_file_rolls_back(admin):
    for bad in ['001,A,2026-02-01,50.01', '001,A,2026-02-01,0.001', '001,A,2026-02-01,0',
                '001,A,2025-12-31,1', '003,A,2026-02-01,1', 'unknown,A,2026-02-01,1',
                '001,A,2026-02-01,1\n001,A,2026-02-02,1', '004,A,2026-02-01,1',
                '001,A,2026-02-01,25\n001,B,2026-02-02,25.01']:
        data = main.ImportCSV(name='invalid', csv_text=ORDERS, refunds_csv_text=HEADER + bad)
        with pytest.raises(HTTPException) as error:
            main.import_csv(data, admin)
        assert error.value.status_code == 422
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(main.Source)) == 0


def test_v2_refund_backfill_is_idempotent(isolated_engine, admin):
    sid = main.import_csv(main.ImportCSV(name='legacy', csv_text=ORDERS), admin)['id']
    # The v2 schema is identical except for the event and reconciliation tables and version marker.
    with isolated_engine.begin() as conn:
        conn.exec_driver_sql('DROP TABLE refund_reconciliations')
        conn.exec_driver_sql('DROP TABLE refund_events')
        conn.exec_driver_sql('UPDATE schema_version SET version=2')
    main.init_db()
    main.init_db()
    with isolated_engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT version FROM schema_version').scalar_one() == 5
        assert conn.exec_driver_sql('SELECT refund_id, amount_cents FROM refund_events').all() == [(None, 500)]
    q = main.Query(metric='refund_amount', source_id=sid, start_date='2026-02-01', end_date='2026-02-01')
    result = main.execute_preview(main.preview(q, admin)['id'], admin)
    assert (result['value'], result['count']) == ('5.00', 1)
