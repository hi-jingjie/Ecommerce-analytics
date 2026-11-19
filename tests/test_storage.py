"""The optional PostgreSQL cases use an isolated schema inside TEST_POSTGRES_URL."""
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from app import main


def test_full_size_import_has_exact_total_and_history(isolated_engine):
    main.init_db()
    header = 'order_id,day,region,channel,amount,status,refund,refund_day\n'
    rows = [f'ORD-{i},2026-01-01,east,web,999999999999.99,paid,999999999999.98,2026-01-02\n' for i in range(9999)]
    rows.append('ORD-last,2026-01-01,east,web,999999999999.98,paid,999999999999.97,2026-01-02\n')
    with main.SessionLocal.begin() as db:
        admin = main.User(username='admin', password_hash='unused', admin=True)
        db.add(admin)
        db.flush()
        caller = {'id': admin.id, 'admin': True, 'region': None}
    source = main.import_csv(main.ImportCSV(name='large', csv_text=header + ''.join(rows)), caller)
    with main.SessionLocal() as db:
        order = db.scalar(select(main.Order).order_by(main.Order.id))
        assert order.order_id == 'ORD-0'
        assert order.order_id_synthetic is False
    base = dict(source_id=source['id'], start_date=date(2026, 1, 1), end_date=date(2026, 1, 2))
    for metric, expected in [('payment_amount', '9999999999999899.99'), ('average_order_value', '999999999999.99'), ('refund_amount', '9999999999999799.99')]:
        plan = main.preview(main.Query(**base, metric=metric), caller)
        result = main.execute_preview(plan['id'], caller)
        assert (result['value'], result['count']) == (expected, 10000)
        with main.SessionLocal() as db:
            history = db.scalar(select(main.History).where(main.History.preview_id == plan['id']))
            assert str(history.value) == expected


def test_order_id_scope_and_money_constraints(isolated_engine):
    main.init_db()
    columns = {col['name'] for col in inspect(isolated_engine).get_columns('orders')}
    assert 'order_id' in columns
    with isolated_engine.begin() as conn:
        conn.execute(text("INSERT INTO sources (id,name,version) VALUES (1,'A',1),(2,'B',1)"))
        insert = text("""INSERT INTO orders(source_id,order_id,order_id_synthetic,day,region,channel,status,amount_cents,refund_cents)
                         VALUES (:sid,:oid,false,'2026-01-01','east','web','paid',:amount,:refund)""")
        for sid in (1, 2):
            conn.execute(insert, dict(sid=sid, oid='001', amount=100, refund=0))
        invalid = [('001', 1, 0), ('negative', -1, 0), ('excess-refund', 10, 11), ('overflow', 10**14, 0)]
        if isolated_engine.dialect.name == 'sqlite':
            invalid.append(('fractional-cent', 0.5, 0))
        for oid, amount, refund in invalid:
            with pytest.raises(IntegrityError), conn.begin_nested():
                conn.execute(insert, dict(sid=1, oid=oid, amount=amount, refund=refund))


def make_v1_database(engine, refund=10):
    # Frozen v1 tables. Keep this fixture independent of current model metadata.
    serial = 'SERIAL' if engine.dialect.name == 'postgresql' else 'INTEGER'
    ddl = [
        "CREATE TABLE bootstrap(id INTEGER PRIMARY KEY)",
        f"CREATE TABLE users(id {serial} PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE,password_hash VARCHAR(200) NOT NULL,region VARCHAR(80),admin BOOLEAN NOT NULL,active BOOLEAN NOT NULL)",
        f"CREATE TABLE sources(id {serial} PRIMARY KEY,name VARCHAR(80) UNIQUE NOT NULL,version INTEGER NOT NULL)",
        "CREATE TABLE grants(user_id INTEGER REFERENCES users(id),source_id INTEGER REFERENCES sources(id),PRIMARY KEY(user_id, source_id))",
        "CREATE TABLE previews(id VARCHAR(64) PRIMARY KEY,user_id INTEGER REFERENCES users(id),payload TEXT,region VARCHAR(80),source_version INTEGER,expires TIMESTAMP,used BOOLEAN)",
        f"CREATE TABLE sessions(id {serial} PRIMARY KEY,digest VARCHAR(64) UNIQUE,user_id INTEGER REFERENCES users(id),expires TIMESTAMP)",
        f"CREATE TABLE orders(id {serial} PRIMARY KEY,source_id INTEGER REFERENCES sources(id),day DATE,region VARCHAR(80),channel VARCHAR(80),status VARCHAR(30),amount NUMERIC(14,2),refund NUMERIC(14,2),refund_day DATE)",
        "CREATE INDEX ix_orders_source_id ON orders(source_id)",
        f"CREATE TABLE query_history(id {serial} PRIMARY KEY,source_id INTEGER,channel VARCHAR(80),region VARCHAR(80),user_id INTEGER REFERENCES users(id),metric VARCHAR(40),start_date DATE,end_date DATE,value NUMERIC(16,2),created_at TIMESTAMP)",
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.exec_driver_sql(statement)
        conn.exec_driver_sql("INSERT INTO users(username,password_hash,admin,active) VALUES ('reader','unused',false,true)")
        conn.exec_driver_sql("INSERT INTO sources(name,version) VALUES ('source',1)")
        conn.exec_driver_sql("INSERT INTO grants VALUES (1,1)")
        conn.execute(text("""INSERT INTO orders(id,source_id,day,region,channel,status,amount,refund,refund_day)
                              VALUES (54,1,'2026-01-01','east','web','paid',19.99,:refund,'2026-01-02')"""), {'refund':refund})
        conn.exec_driver_sql("""INSERT INTO query_history(id,source_id,user_id,metric,start_date,end_date,value,created_at)
                                VALUES (77,1,1,'payment_amount','2026-01-01','2026-01-01',19.99,'2026-01-03 10:00:00')""")


def test_legacy_upgrade_preserves_values_and_marks_unknown_ids(isolated_engine):
    make_v1_database(isolated_engine)
    with isolated_engine.begin() as conn:
        conn.exec_driver_sql("""INSERT INTO orders(id,day,region,channel,status,amount,refund)
                                VALUES (55,'2026-01-01','east','web','paid',1.01,0)""")
    main.init_db()
    main.init_db()
    with main.SessionLocal() as db:
        unscoped_source = db.get(main.Order, 55).source_id
        assert db.get(main.Source, unscoped_source).name == 'v1 未归属订单'
        assert db.get(main.Grant, (1, unscoped_source)) is None
        assert db.get(main.Grant, (1, 1)) is not None
        order = db.get(main.Order, 54)
        assert getattr(order, 'order_id_synthetic', None) is True
        assert (order.order_id, order.amount_cents, order.refund_cents) == ('legacy-54', 1999, 1000)
        saved = db.get(main.History, 77)
        assert (saved.value, saved.matched_count, saved.source_name) == (Decimal('19.99'), None, None)
        next_history = main.History(user_id=1, source_id=1, metric='paid_orders', start_date=order.day, end_date=order.day, value=Decimal('1.00'))
        db.add(next_history)
        db.commit()
        assert next_history.id > 77


def test_invalid_legacy_upgrade_rolls_back(isolated_engine):
    make_v1_database(isolated_engine, refund=20)
    with pytest.raises((RuntimeError, IntegrityError)):
        main.init_db()
    with isolated_engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT id, amount, refund FROM orders').one() == (54, 19.99 if isolated_engine.dialect.name == 'sqlite' else Decimal('19.99'), 20)
        assert conn.exec_driver_sql('SELECT id FROM query_history').scalar_one() == 77
        assert 'schema_version' not in inspect(conn).get_table_names()


def test_upgrade_preserves_displayed_legacy_sqlite_history(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path}/legacy.db')
    make_v1_database(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql('UPDATE query_history SET value=70368744177664.09')
    monkeypatch.setattr(main, 'engine', engine)
    main.init_db()
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT value_cents FROM query_history').scalar_one() == 7036874417766409


@pytest.mark.parametrize('custom_ddl', [
    'CREATE INDEX custom_orders_index ON orders(day)',
    'CREATE TRIGGER custom_order_trigger AFTER DELETE ON orders BEGIN SELECT 1; END',
    'CREATE VIEW order_summary AS SELECT amount FROM orders',
    'CREATE TABLE attached_notes(id INTEGER PRIMARY KEY, order_pk INTEGER REFERENCES orders(id) ON DELETE CASCADE)',
])
def test_upgrade_preserves_unknown_sqlite_dependencies(tmp_path, monkeypatch, custom_ddl):
    engine = create_engine(f'sqlite:///{tmp_path}/custom.db')
    make_v1_database(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(custom_ddl)
    monkeypatch.setattr(main, 'engine', engine)
    with pytest.raises(RuntimeError):
        main.init_db()
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT amount FROM orders WHERE id=54').scalar_one() == 19.99
        assert conn.execute(text('SELECT count(*) FROM sqlite_master WHERE sql=:ddl'), {'ddl':custom_ddl}).scalar_one() == 1
