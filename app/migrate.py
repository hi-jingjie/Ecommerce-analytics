"""Transactional database initialization and upgrade for the supported v1 and v2 schemas."""
import re
from decimal import Decimal

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, select, text
from sqlalchemy.schema import AddConstraint, CreateColumn

from app.models import Base, History, Order, RefundEvent, SchemaVersion, Source


VERSION = 3
# Frozen v1 contract. Verify before rebuilding tables so unknown columns are never lost.
V1_COLUMNS = {
    'bootstrap': 'id',
    'users': 'id username password_hash region admin active',
    'sessions': 'id digest user_id expires',
    'sources': 'id name version',
    'orders': 'id source_id day region channel status amount refund refund_day',
    'grants': 'user_id source_id',
    'previews': 'id user_id payload region source_version expires used',
    'query_history': 'id source_id channel region user_id metric start_date end_date value created_at',
}


def _assign_unscoped_orders(conn):
    # v1 permitted a NULL source. Keep these orders in a new, ungranted snapshot.
    if not conn.exec_driver_sql('SELECT 1 FROM orders WHERE source_id IS NULL LIMIT 1').first():
        return
    name = 'v1 未归属订单'
    suffix = 0
    while conn.scalar(select(Source.id).where(Source.name == name)) is not None:
        suffix += 1
        name = f'v1 未归属订单 ({suffix})'
    source_id = conn.execute(Source.__table__.insert().values(name=name, version=1)).inserted_primary_key[0]
    conn.execute(text('UPDATE orders SET source_id = :sid WHERE source_id IS NULL'), {'sid': source_id})


def _check_sqlite_dependencies(conn):
    inspector = inspect(conn)
    rebuilt = {'orders', 'query_history'}
    table_pattern = re.compile(r'\b(orders|query_history)\b', re.IGNORECASE)
    for kind, name, table, ddl in conn.exec_driver_sql(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE type IN ('index','trigger','view')"
    ):
        if kind == 'index':
            if table.lower() not in rebuilt:
                continue
            if name == 'ix_orders_source_id' and table == 'orders':
                indexes = inspector.get_indexes('orders')
                if any(i['name'] == name and i['column_names'] == ['source_id']
                       and not i['unique'] and not i['dialect_options'] for i in indexes):
                    continue
        elif table.lower() not in rebuilt and not table_pattern.search(ddl or ''):
            continue
        raise RuntimeError(f'自定义 SQLite 对象 {name} 涉及待重建表，请先在副本上适配')
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            if fk['referred_table'].lower() in rebuilt:
                raise RuntimeError(f'SQLite 表 {table} 的外键引用待重建表，请先在副本上适配')


def _sqlite_cents(value, history=False):
    # The Python SQLite driver returns the stored integer or double. Parsing the
    # shortest round-tripping decimal avoids another float multiplication.
    if history and isinstance(value, float):
        # v1 SQLAlchemy Numeric(16,2) formatted the stored double to two places.
        amount = Decimal(format(value, '.2f'))
    else:
        amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0 or amount > Decimal('92233720368547758.07'):
        raise ValueError('金额超出存储范围')
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError('金额存在小于一分的精度')
    return int(cents)


def _sqlite_copy(conn, model, money_columns):
    table = model.__tablename__
    staging = f'v1_{table}_copy'
    conn.exec_driver_sql(f'CREATE TEMPORARY TABLE {staging} AS SELECT * FROM {table}')
    conn.exec_driver_sql(f'DROP TABLE {table}')
    model.__table__.create(conn)
    old_columns = V1_COLUMNS[table].split()
    new_columns = [f'{c}_cents' if c in money_columns else c for c in old_columns]
    if model is Order:
        new_columns.extend(['order_id', 'order_id_synthetic'])
    columns = ', '.join(new_columns)
    params = ', '.join(f':{c}' for c in new_columns)
    insert = text(f'INSERT INTO {table} ({columns}) VALUES ({params})')
    with conn.exec_driver_sql(f'SELECT * FROM {staging}').mappings() as result:
        for batch in result.partitions(1000):
            converted = []
            for old in batch:
                row = dict(old)
                try:
                    for c in money_columns:
                        row[f'{c}_cents'] = _sqlite_cents(row.pop(c), history=model is History)
                except (ArithmeticError, ValueError) as exc:
                    raise ValueError(f'无效金额，数据表 {table}，主键 {row["id"]}') from exc
                if model is Order:
                    row.update(order_id=f'legacy-{row["id"]}', order_id_synthetic=True)
                converted.append(row)
            conn.execute(insert, converted)
    conn.exec_driver_sql(f'DROP TABLE {staging}')


def _postgres_sync_sequence(conn, table):
    # Imported v1 rows can have explicit IDs above the next sequence value.
    # Use transactional RESTART only when needed, keeping a higher sequence.
    sequence = conn.execute(text("""
        SELECT n.nspname, c.relname
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.oid = pg_get_serial_sequence(:table, 'id')::regclass
    """), {'table': table}).one_or_none()
    max_id = conn.exec_driver_sql(f'SELECT MAX(id) FROM {table}').scalar_one()
    if not sequence or max_id is None:
        return
    quote = conn.dialect.identifier_preparer.quote
    name = '.'.join(quote(part) for part in sequence)
    last, called = conn.exec_driver_sql(f'SELECT last_value, is_called FROM {name}').one()
    if last < max_id or (last == max_id and not called):
        conn.exec_driver_sql(f'ALTER SEQUENCE {name} RESTART WITH {int(max_id) + 1}')


def _postgres_upgrade(conn):
    # ALTER preserves the original tables, sequences, ownership and privileges.
    for table, columns in [('orders', ['amount', 'refund']), ('query_history', ['value'])]:
        for column in columns:
            conn.exec_driver_sql(f'ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT USING ({column} * 100)::bigint')
            conn.exec_driver_sql(f'ALTER TABLE {table} RENAME COLUMN {column} TO {column}_cents')

    conn.exec_driver_sql('ALTER TABLE orders ADD COLUMN order_id VARCHAR(128), ADD COLUMN order_id_synthetic BOOLEAN')
    conn.exec_driver_sql("UPDATE orders SET order_id = 'legacy-' || id, order_id_synthetic = true")
    conn.exec_driver_sql('''ALTER TABLE orders ALTER COLUMN source_id SET NOT NULL,
                           ALTER COLUMN order_id SET NOT NULL, ALTER COLUMN order_id_synthetic SET NOT NULL''')
    for column in History.__table__.columns:
        if column.name not in V1_COLUMNS['query_history'].split() and column.name != 'value_cents':
            definition = CreateColumn(column).compile(dialect=conn.dialect)
            conn.exec_driver_sql(f'ALTER TABLE query_history ADD COLUMN {definition}')
    for table in (Order.__table__, History.__table__):
        for constraint in table.constraints:
            if isinstance(constraint, (CheckConstraint, UniqueConstraint)) and constraint.name not in {
                'ck_orders_integer_cents', 'ck_history_integer_cents',
            }:
                conn.execute(AddConstraint(constraint, isolate_from_table=False))
    for index in History.__table__.indexes:
        index.create(conn)


def _upgrade_v1(conn):
    inspector = inspect(conn)
    for table, columns in V1_COLUMNS.items():
        if not inspector.has_table(table) or {c['name'] for c in inspector.get_columns(table)} != set(columns.split()):
            raise RuntimeError(f'无法识别 v1 数据表 {table}，请核查备份与数据库结构')
    if conn.dialect.name == 'postgresql':
        conn.exec_driver_sql('LOCK TABLE orders, query_history, sources IN ACCESS EXCLUSIVE MODE')
        for table in ['orders', 'query_history', 'sources']:
            _postgres_sync_sequence(conn, table)
    else:
        _check_sqlite_dependencies(conn)
    _assign_unscoped_orders(conn)
    if conn.dialect.name == 'sqlite':
        _sqlite_copy(conn, Order, ('amount', 'refund'))
        _sqlite_copy(conn, History, ('value',))
    else:
        _postgres_upgrade(conn)


def _copy_legacy_refunds(conn):
    conn.exec_driver_sql("""
        INSERT INTO refund_events (order_pk, day, amount_cents)
        SELECT id, refund_day, refund_cents FROM orders WHERE refund_day IS NOT NULL
    """)


def upgrade(engine):
    """Upgrade a fresh or recognized application database in one locked transaction."""
    with engine.begin() as conn:
        if conn.dialect.name == 'sqlite':
            # sqlite3 legacy transaction control otherwise auto-commits early DDL.
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        elif conn.dialect.name == 'postgresql':
            conn.exec_driver_sql('SELECT pg_advisory_xact_lock(683173401, 2)')
        else:
            raise RuntimeError('只支持 SQLite 和 PostgreSQL')

        inspector = inspect(conn)
        tables = set(inspector.get_table_names())
        if 'schema_version' in tables:
            versions = conn.execute(select(SchemaVersion.id, SchemaVersion.version)).all()
            if versions not in ([(1, 2)], [(1, VERSION)]):
                raise RuntimeError(f'不支持的数据库版本 {versions!r}')
            upgrading = versions == [(1, 2)]
            for table in Base.metadata.tables.values():
                if upgrading and table is RefundEvent.__table__:
                    if table.name in tables:
                        raise RuntimeError('v2 库已有 refund_events 表，请核查结构')
                    continue
                if table.name not in tables or not set(table.c.keys()) <= {c['name'] for c in inspector.get_columns(table.name)}:
                    raise RuntimeError(f'数据库版本标记与数据表 {table.name} 不一致')
            if upgrading:
                RefundEvent.__table__.create(conn)
                _copy_legacy_refunds(conn)
                conn.execute(SchemaVersion.__table__.update().where(SchemaVersion.id == 1).values(version=VERSION))
            return VERSION

        legacy = bool(tables & set(Base.metadata.tables))
        if legacy:
            if tables & (set(Base.metadata.tables) - V1_COLUMNS.keys()):
                raise RuntimeError('数据库缺少 schema_version，请核查备份与数据库结构')
            _upgrade_v1(conn)
        Base.metadata.create_all(conn)
        if legacy:
            _copy_legacy_refunds(conn)
        conn.execute(SchemaVersion.__table__.insert().values(id=1, version=VERSION))
        return VERSION


if __name__ == '__main__':
    from app.main import engine
    print(f'数据库结构版本 {upgrade(engine)}')
