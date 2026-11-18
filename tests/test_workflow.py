import asyncio
import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import main


def test_permissions_preview_and_refund_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'engine', create_engine(f'sqlite:///{tmp_path}/app.db'))
    monkeypatch.setattr(main, 'SessionLocal', sessionmaker(bind=main.engine))
    monkeypatch.setenv('SETUP_TOKEN', 'setup-test')
    main.init_db()

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as c:
            credentials = {'username': 'admin', 'password': 'long-password-123'}
            assert (await c.post('/api/setup', json=credentials)).status_code == 403
            assert (await c.post('/api/setup', json=credentials, headers={'X-Setup-Token': 'setup-test'})).status_code == 201
            token = (await c.post('/api/login', json=credentials)).json()['access_token']
            admin = {'Authorization': 'Bearer '+token}
            await c.post('/api/demo', headers=admin)
            sources = (await c.get('/api/sources', headers=admin)).json()
            source = sources[0]['id']
            created = await c.post('/api/users', headers=admin, json={'username':'viewer','password':'viewer-password-123','region':'华东','source_ids':[source]})
            assert created.status_code == 201, created.text
            viewer_id = created.json()['id']
            vtoken = (await c.post('/api/login', json={'username':'viewer','password':'viewer-password-123'})).json()['access_token']
            viewer = {'Authorization': 'Bearer '+vtoken}
            assert (await c.post('/api/demo', headers=viewer)).status_code == 403
            params = {'source_id':source,'metric':'payment_amount','start_date':'2026-01-01','end_date':'2026-01-31'}
            preview = (await c.post('/api/previews', headers=viewer, json=params)).json()
            assert preview['region'] == '华东'
            result = await c.post('/api/previews/'+preview['id']+'/execute', headers=viewer)
            assert result.json()['value'] == '100.00', result.text
            assert (await c.post('/api/previews/'+preview['id']+'/execute', headers=admin)).status_code == 404
            assert (await c.post('/api/previews/'+preview['id']+'/execute', headers=viewer)).status_code == 409
            preview = (await c.post('/api/previews', headers=viewer, json=params)).json()
            await c.put(f'/api/users/{viewer_id}/access', headers=admin, json={'region':'华北','source_ids':[source],'active':True})
            assert (await c.post('/api/previews/'+preview['id']+'/execute', headers=viewer)).status_code == 409
            refunds = {**params, 'metric':'refund_amount','start_date':'2026-02-01','end_date':'2026-02-28'}
            p = (await c.post('/api/previews', headers=admin, json=refunds)).json()
            assert (await c.post('/api/previews/'+p['id']+'/execute', headers=admin)).json()['value'] == '20.00'
            assert len((await c.get('/api/history', headers=viewer)).json()) == 1
            assert (await c.get('/api/health')).status_code == 200
            assert (await c.post('/api/logout', headers=viewer)).status_code == 200
            assert (await c.get('/api/history', headers=viewer)).status_code == 401
    asyncio.run(run())
