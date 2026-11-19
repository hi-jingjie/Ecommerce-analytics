from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main


def test_preview_applies_region_and_channel(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path}/metrics.db')
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(main, 'SessionLocal', sessionmaker(bind=engine))
    main.init_db()
    with main.SessionLocal() as db:
        db.add(main.User(id=1, username='owner', password_hash='unused', admin=True))
        db.add(main.User(id=2, username='viewer', password_hash='unused', region='east'))
        db.commit()
    data = main.ImportCSV(name='orders', csv_text=(
        'order_id,day,region,channel,amount,status,refund,refund_day\n'
        'A,2026-01-01,east,web,10.10,paid,0,\n'
        'B,2026-01-02,west,web,90.00,paid,0,\n'
        'C,2026-01-02,east,store,70.00,paid,0,\n'
    ))
    admin = {'id': 1, 'admin': True, 'region': None}
    viewer = {'id': 2, 'admin': False, 'region': 'east'}
    source_id = main.import_csv(data, admin)['id']
    main.update_access(2, main.Access(source_ids=[source_id], region='east'), admin)
    request = main.Query(source_id=source_id, metric='payment_amount',
                         start_date=date(2026, 1, 1), end_date=date(2026, 1, 2), channel='web')
    plan = main.preview(request, viewer)
    result = main.execute_preview(plan['id'], viewer)
    assert result['value'] == '10.10'
    assert result['count'] == 1
    engine.dispose()
