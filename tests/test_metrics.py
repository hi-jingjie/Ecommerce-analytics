from datetime import date
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import main


def test_aggregation_applies_region_and_channel(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path}/metrics.db')
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(main, 'SessionLocal', sessionmaker(bind=engine))
    main.init_db()
    with main.SessionLocal() as db:
        db.add(main.Source(id=1, name='test'))
        db.add(main.User(id=1, username='tester', password_hash='unused', region='east'))
        db.add_all([
            main.Order(source_id=1, day=date(2026, 1, 1), region='east', channel='web', status='paid', amount=Decimal('10.10'), refund=Decimal('1.00')),
            main.Order(source_id=1, day=date(2026, 1, 2), region='west', channel='web', status='paid', amount=Decimal('90.00'), refund=Decimal('0')),
        ])
        db.commit()
    request = main.Query(metric='payment_amount', start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), channel='web')
    result = main.query(request, {'id': 1, 'region': 'east', 'admin': False})
    assert result['value'] == 10.1
    assert result['count'] == 1
