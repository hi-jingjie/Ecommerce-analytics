from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select

from app import main


@pytest.fixture
def database(isolated_engine, monkeypatch):
    monkeypatch.setenv('SETUP_TOKEN', 'test-setup')
    main.init_db()
    return isolated_engine


@pytest.fixture
def viewer_preview(database):
    with main.SessionLocal.begin() as db:
        admin = main.User(username='admin', password_hash='unused', admin=True)
        viewer = main.User(username='viewer', password_hash='unused', region='east')
        source = main.Source(name='orders')
        db.add_all([admin, viewer, source])
        db.flush()
        uid, sid = viewer.id, source.id
        db.add(main.Grant(user_id=uid, source_id=sid))
        admin_identity = {'id': admin.id, 'admin': True, 'region': None}
    identity = {'id': uid, 'region': 'east', 'admin': False}
    params = main.Query(source_id=sid, metric='paid_orders', start_date='2026-01-01',
                        end_date='2026-01-31')
    pid = main.preview(params, identity)['id']
    return admin_identity, identity, pid


def status(success_code, endpoint, *args):
    try:
        endpoint(*args)
        return success_code
    except HTTPException as exc:
        return exc.status_code


@contextmanager
def on_statement(engine, fn):
    event.listen(engine, 'before_cursor_execute', fn)
    try:
        yield
    finally:
        event.remove(engine, 'before_cursor_execute', fn)


def signal_contender(engine, worker_name, ready):
    def at_write(conn, cursor, statement, parameters, context, executemany):
        sql = statement.upper().lstrip()
        if threading.current_thread().name.startswith(worker_name) and (
            sql.startswith(('BEGIN IMMEDIATE', 'UPDATE ', 'DELETE ')) or 'FOR UPDATE' in sql
        ):
            ready.set()

    return on_statement(engine, at_write)


def run_together(engine, first, second):
    # Both handlers have checked out a connection before either touches the data.
    barrier = threading.Barrier(2, timeout=10)
    seen = threading.local()

    def first_statement(*args):
        if not getattr(seen, 'started', False):
            seen.started = True
            barrier.wait()

    with on_statement(engine, first_statement), ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(call) for call in (first, second)]
        return sorted(f.result(timeout=15) for f in futures)


def test_concurrent_setup_creates_one_admin(database):
    def setup(username):
        credentials = main.Credentials(username=username, password='test-password-123')
        return status(201, main.setup, credentials, 'test-setup')

    responses = run_together(database, lambda: setup('first'), lambda: setup('second'))
    assert responses in ([201, 403], [201, 409])
    with main.SessionLocal() as db:
        assert db.scalars(select(main.User.admin)).all() == [True]
        assert db.scalars(select(main.Bootstrap.id)).all() == [1]


def test_concurrent_execute_creates_one_history(database, viewer_preview):
    _, viewer, pid = viewer_preview

    def execute():
        return status(200, main.execute_preview, pid, viewer)

    assert run_together(database, execute, execute) == [200, 409]
    with main.SessionLocal() as db:
        assert db.get(main.Preview, pid).used
        assert db.scalars(select(main.History.user_id)).all() == [viewer['id']]


def test_revocation_in_progress_orders_before_execute(database, viewer_preview, monkeypatch):
    admin, viewer, pid = viewer_preview
    revoke_written = threading.Event()
    execute_at_write = threading.Event()
    original = main.set_grants

    def hold_revocation(db, uid, ids):
        original(db, uid, ids)
        revoke_written.set()
        assert execute_at_write.wait(10), 'execute failed to reach a write or lock'

    # The revocation has reached the database and remains uncommitted while
    # the other connection attempts its first lock or conditional write.
    monkeypatch.setattr(main, 'set_grants', hold_revocation)
    with signal_contender(database, 'execute', execute_at_write), ThreadPoolExecutor(1) as mutator, \
            ThreadPoolExecutor(1, thread_name_prefix='execute') as reader:
        revoke = mutator.submit(main.update_access, viewer['id'],
                                main.Access(region='east', source_ids=[]), admin)
        assert revoke_written.wait(10)
        execute = reader.submit(status, 200, main.execute_preview, pid, viewer)
        revoke.result(timeout=15)
        assert execute.result(timeout=15) == 403
    with main.SessionLocal() as db:
        assert not db.get(main.Preview, pid).used
        assert db.scalar(select(func.count()).select_from(main.History)) == 0


def test_execution_in_progress_finishes_before_revocation(database, viewer_preview, monkeypatch):
    admin, viewer, pid = viewer_preview
    aggregating = threading.Event()
    revoke_at_write = threading.Event()
    original = main.aggregate

    def hold_execution(db, data, user):
        aggregating.set()
        assert revoke_at_write.wait(10), 'revoke failed to reach a write or lock'
        return original(db, data, user)

    monkeypatch.setattr(main, 'aggregate', hold_execution)
    with signal_contender(database, 'revoke', revoke_at_write), ThreadPoolExecutor(1) as reader, \
            ThreadPoolExecutor(1, thread_name_prefix='revoke') as mutator:
        execute = reader.submit(main.execute_preview, pid, viewer)
        assert aggregating.wait(10)
        revoke = mutator.submit(main.update_access, viewer['id'],
                                main.Access(region='east', source_ids=[]), admin)
        assert execute.result(timeout=15)['count'] == 0
        revoke.result(timeout=15)
    with main.SessionLocal() as db:
        assert db.scalars(select(main.History.preview_id)).all() == [pid]
        assert db.scalar(select(func.count()).select_from(main.Grant)) == 0
        actions = db.scalars(select(main.AuditEvent.action).order_by(main.AuditEvent.id)).all()
        assert actions.index('query.execute') < actions.index('user.access')


def test_aggregate_failure_rolls_back_claim(database, viewer_preview, monkeypatch):
    _, viewer, pid = viewer_preview

    def fail_aggregate(*args):
        raise RuntimeError('simulated aggregate failure')

    with monkeypatch.context() as patch:
        patch.setattr(main, 'aggregate', fail_aggregate)
        with pytest.raises(RuntimeError, match='simulated aggregate failure'):
            main.execute_preview(pid, viewer)
    with main.SessionLocal() as db:
        assert not db.get(main.Preview, pid).used
        assert db.scalar(select(func.count()).select_from(main.History)) == 0
    assert status(200, main.execute_preview, pid, viewer) == 200
