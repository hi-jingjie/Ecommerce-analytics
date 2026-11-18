import os
os.environ['DATABASE_URL'] = 'sqlite:///./test.db'

import asyncio
import httpx
from app.main import app, init_db

def test_query_flow(tmp_path, monkeypatch):
    from app import main
    from sqlalchemy import create_engine
    engine = create_engine(f'sqlite:///{tmp_path}/test.db')
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(main, 'SessionLocal', main.sessionmaker(bind=engine))
    monkeypatch.setenv('SETUP_TOKEN', 'test-setup')
    init_db()
    async def exercise():
      async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.post('/api/setup', json={'username': 'admin', 'password': 'long-password-123'}, headers={'X-Setup-Token':'test-setup'})).status_code == 201
        token = (await client.post('/api/login', json={'username': 'admin', 'password': 'long-password-123'})).json()['access_token']
        headers = {'Authorization': 'Bearer ' + token}
        assert (await client.post('/api/demo', headers=headers)).status_code == 200
        query = {'metric': 'payment_amount', 'start_date': '2026-01-01', 'end_date': '2026-12-31'}
        plan = (await client.post('/api/previews', json=query, headers=headers)).json()
        result = await client.post('/api/previews/'+plan['id']+'/execute', headers=headers)
        assert result.status_code == 200
        assert result.json()['value'] == '300.00'
        assert len((await client.get('/api/history', headers=headers)).json()) == 1
        assert (await client.post('/api/previews', json={**query, 'metric': 'unknown'}, headers=headers)).status_code == 422
        assert (await client.get('/api/history')).status_code == 401
    asyncio.run(exercise())
