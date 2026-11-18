import asyncio
from datetime import datetime, timedelta
import httpx
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from app import main

@pytest.mark.parametrize('bad', ['negative', 'duplicate', 'date', 'precision', 'nan', 'empty'])
def test_invalid_import_is_atomic(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(main, 'engine', create_engine(f'sqlite:///{tmp_path}/app.db'))
    monkeypatch.setattr(main, 'SessionLocal', sessionmaker(bind=main.engine))
    main.init_db()
    header = 'order_id,day,region,channel,amount,status,refund,refund_day\n'
    good = '1,2026-01-01,east,web,100,paid,0,\n'
    invalid = {'negative':'2,2026-01-01,east,web,-1,paid,0,\n','duplicate':good,'date':'2,2026-01-01,east,web,10,paid,1,2025-01-01\n','precision':'2,2026-01-01,east,web,1.001,paid,0,\n','nan':'2,2026-01-01,east,web,NaN,paid,0,\n','empty':''}[bad]
    with pytest.raises(main.HTTPException) as exc:
        main.import_csv(main.ImportCSV(name='bad', csv_text=header+(good+invalid if bad!='empty' else '')), {'admin':True})
    assert exc.value.status_code == 422
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(main.Source)) == 0

def test_import_preview_expiry_and_revocation(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'engine', create_engine(f'sqlite:///{tmp_path}/app.db'))
    monkeypatch.setattr(main, 'SessionLocal', sessionmaker(bind=main.engine))
    monkeypatch.setenv('SETUP_TOKEN','setup-test')
    main.init_db()
    async def run():
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as c:
        creds={'username':'admin','password':'long-password-123'}
        await c.post('/api/setup',json=creds,headers={'X-Setup-Token':'setup-test'})
        a={'Authorization':'Bearer '+(await c.post('/api/login',json=creds)).json()['access_token']}
        data={'name':'csv','csv_text':'order_id,day,region,channel,amount,status,refund,refund_day\n1,2026-01-01,east,web,100,paid,0,\n'}
        sid=(await c.post('/api/sources/import',headers=a,json=data)).json()['id']
        assert (await c.post('/api/sources/import',headers=a,json=data)).status_code==409
        uid=(await c.post('/api/users',headers=a,json={'username':'analyst','password':'long-password-123','source_ids':[sid]})).json()['id']
        v={'Authorization':'Bearer '+(await c.post('/api/login',json={'username':'analyst','password':'long-password-123'})).json()['access_token']}
        q={'source_id':sid,'metric':'payment_amount','start_date':'2026-01-01','end_date':'2026-01-31','channel':"web' OR 1=1 --"}
        p=(await c.post('/api/previews',headers=v,json=q)).json()
        assert (await c.post('/api/previews/'+p['id']+'/execute',headers=v)).json()['value']=='0.00'
        p=(await c.post('/api/previews',headers=v,json=q)).json()
        with main.SessionLocal() as db:
            db.get(main.Preview,p['id']).expires=datetime.utcnow()-timedelta(seconds=1);db.commit()
        assert (await c.post('/api/previews/'+p['id']+'/execute',headers=v)).status_code==409
        p=(await c.post('/api/previews',headers=v,json=q)).json()
        await c.put(f'/api/users/{uid}/access',headers=a,json={'source_ids':[]})
        assert (await c.post('/api/previews/'+p['id']+'/execute',headers=v)).status_code==403
        assert (await c.get('/api/sources',headers=v)).json()==[]
        assert (await c.get('/')).status_code==200
        assert (await c.get('/static/app.js')).status_code==200
    asyncio.run(run())
