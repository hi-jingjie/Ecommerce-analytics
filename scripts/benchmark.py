"""Measure local SQLite import and aggregate costs on a disposable order snapshot."""
import json
import math
import platform
import sqlite3
import sys
from datetime import date
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.metrics import aggregate
from app.migrate import upgrade
from app.models import Source
from app.orders import parse_orders
from app.schemas import Query


def run(folder):
    rows = 10_000
    samples = 50
    csv = 'order_id,day,region,channel,amount,status,refund,refund_day\n' + ''.join(
        f'{i},2026-01-01,{"east" if i % 2 else "west"},web,19.99,paid,0,\n'
        for i in range(rows)
    )
    engine = create_engine(f'sqlite:///{folder}/benchmark.db')
    upgrade(engine)
    with Session(engine) as db:
        start = perf_counter()
        orders = parse_orders(csv)
        source = Source(name='benchmark')
        db.add(source)
        db.flush()
        sid = source.id
        for order in orders:
            order.source_id = sid
        db.add_all(orders)
        db.commit()
        import_ms = (perf_counter() - start) * 1000
        query = Query(source_id=sid, metric='payment_amount', start_date=date(2026, 1, 1),
                      end_date=date(2026, 1, 31), channel='web')
        region = {'region': 'east'}
        value, matched = aggregate(db, query, region)
        assert str(value) == '99950.00' and matched == 5000
        timings = []
        for _ in range(samples):
            start = perf_counter()
            aggregate(db, query, region)
            timings.append((perf_counter() - start) * 1000)
    engine.dispose()
    return {
        'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version, 'input_rows': rows,
        'import_ms': round(import_ms, 2), 'matched': matched, 'result': str(value),
        'warm_samples': samples, 'aggregate_p50_ms': round(median(timings), 2),
        'aggregate_p95_ms': round(sorted(timings)[math.ceil(samples * 0.95) - 1], 2),
    }


if __name__ == '__main__':
    with TemporaryDirectory(prefix='analytics-benchmark-') as folder:
        print(json.dumps(run(folder), indent=2))
