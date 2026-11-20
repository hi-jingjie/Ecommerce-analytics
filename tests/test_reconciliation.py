from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app import main

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'
HEADER = 'order_id,refund_id,refund\n'
ROWS = ['demo-001,R-02,18.99', 'unknown,OUT,1.50', 'demo-002,OLD,20.00']


@pytest.fixture
def context(isolated_engine):
    main.init_db()
    with main.SessionLocal.begin() as db:
        owner = main.User(username='owner', password_hash='unused', admin=True)
        db.add(owner)
        db.flush()
        admin = {'id': owner.id, 'region': None, 'admin': True}
    source = main.import_csv(main.ImportCSV(name='snapshot', csv_text=(EXAMPLES/'orders.csv').read_text(),
                                            refunds_csv_text=(EXAMPLES/'refunds.csv').read_text()), admin)
    return source['id'], admin


def run(sid, user, rows):
    return main.reconcile_refunds(sid, main.ChannelBill(csv_text=HEADER+'\n'.join(rows)), user)


def test_report_types_and_idempotency(context):
    sid, admin = context
    report = run(sid, admin, ROWS)
    assert report['system_total'] == '50.00'
    assert report['channel_total'] == '40.49'
    assert report['difference'] == '9.51'
    assert report['matched'] == 0
    by_key = {(i['order_id'], i['refund_id']): i for i in report['items']}
    assert by_key[('demo-001', 'R-01')]['status'] == 'system_only'
    assert by_key[('demo-001', 'R-02')]['difference'] == '1.00'
    assert by_key[('unknown', 'OUT')]['status'] == 'channel_only'
    assert by_key[('demo-002', None)]['status'] == 'review'
    assert by_key[('demo-002', 'OLD')]['status'] == 'review'
    assert run(sid, admin, [r.replace('20.00', '20.0') for r in reversed(ROWS)]) == report
    assert main.get_reconciliation(report['id'], admin) == report
    corrected = run(sid, admin, ['demo-001,R-02,19.99'])
    assert corrected['id'] != report['id'] and corrected['matched'] == 1
    other = main.import_csv(main.ImportCSV(name='other', csv_text=(EXAMPLES/'orders.csv').read_text()), admin)
    other_report = run(other['id'], admin, ROWS)
    assert other_report['id'] != report['id'] and other_report['system_total'] == '20.00'


def test_permissions_and_bad_bills(context):
    sid, admin = context
    viewer = {**admin, 'admin': False}
    for operation in [lambda: run(sid, viewer, []), lambda: main.get_reconciliation(1, viewer)]:
        with pytest.raises(HTTPException) as err:
            operation()
        assert err.value.status_code == 403
    for rows in [[ROWS[0], ROWS[0]], ['001,R,10.001'], ['001,,12'], ['001,R,-1']]:
        with pytest.raises(HTTPException) as err:
            run(sid, admin, rows)
        assert err.value.status_code == 422
    assert run(sid, admin, [])['channel_total'] == '0.00'


def test_concurrent_upload(context):
    sid, admin = context
    barrier = Barrier(2)
    def upload():
        barrier.wait(timeout=5)
        return run(sid, admin, ROWS)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(upload) for _ in range(2)]
        first, second = [job.result(timeout=15) for job in jobs]
    assert first == second
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(main.Reconciliation)) == 1


def test_upgrade_from_v3(isolated_engine, context):
    sid, admin = context
    with isolated_engine.begin() as conn:
        conn.exec_driver_sql('DROP TABLE refund_reconciliations')
        conn.exec_driver_sql('UPDATE schema_version SET version=3')
    main.init_db()
    report = run(sid, admin, ROWS)
    main.init_db()
    assert main.get_reconciliation(report['id'], admin) == report
    assert report['system_total'] == '50.00'
