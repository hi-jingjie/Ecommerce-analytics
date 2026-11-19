import asyncio
import hashlib
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import main


@pytest.fixture
def database(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path}/audit.db')
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(main, 'SessionLocal', sessionmaker(bind=engine))
    main.init_db()
    with main.SessionLocal.begin() as db:
        db.add_all([
            main.User(id=1, username='admin', password_hash='unused', admin=True),
            main.User(id=2, username='viewer', password_hash='unused', region='华东'),
            main.Source(id=1, name='订单快照', version=4),
        ])
        db.flush()
        db.add(main.Grant(user_id=2, source_id=1))
        for uid, token in [(1, 'admin-token'), (2, 'viewer-token')]:
            db.add(main.Token(user_id=uid, digest=hashlib.sha256(token.encode()).hexdigest(),
                              expires=datetime.utcnow() + timedelta(hours=1)))
    yield main.SessionLocal
    engine.dispose()


ADMIN = {'Authorization': 'Bearer admin-token'}
VIEWER = {'Authorization': 'Bearer viewer-token'}
QUERY = {'metric': 'payment_amount', 'start_date': '2026-01-01',
         'end_date': '2026-01-31', 'channel': 'web', 'source_id': 1}


def test_history_context_keyset_and_isolation(database):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as client:
            async def execute(headers):
                preview = (await client.post('/api/previews', headers=headers, json=QUERY)).json()
                result = await client.post(f"/api/previews/{preview['id']}/execute", headers=headers)
                assert result.status_code == 200
                return preview['id']

            await execute(VIEWER)
            await execute(ADMIN)
            pid = await execute(VIEWER)
            page = (await client.get('/api/history?limit=1', headers=VIEWER)).json()
            newest, = page['items']
            assert newest['preview_id'] == pid
            assert newest['source_name'] == '订单快照' and newest['source_version'] == 4
            assert newest['definition_version'] == 1 and newest['source_id'] == 1
            assert newest['channel'] == 'web' and newest['region'] == '华东'
            assert newest['matched_count'] == 0 and newest['duration_ms'] >= 0
            assert newest['created_at'] and newest['date_basis'] == '支付日'
            await execute(VIEWER)
            older = (await client.get('/api/history', headers=VIEWER,
                                      params={'before_id': page['next_cursor'], 'limit': 1})).json()
            assert len(older['items']) == 1 and older['next_cursor'] is None
            assert older['items'][0]['id'] < newest['id']
            assert older['items'][0]['region'] == '华东'
            assert len((await client.get('/api/history', headers=VIEWER)).json()) == 3
            assert (await client.get('/api/history?limit=101', headers=VIEWER)).status_code == 422
            assert (await client.get('/api/history?before_id=0', headers=VIEWER)).status_code == 422
    asyncio.run(run())


def test_admin_and_query_audit_rollback_and_redaction(database, monkeypatch):
    def fail(*args):
        raise RuntimeError('internal-sensitive-database-error')

    async def run():
        transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            assert (await client.get('/api/audit', headers=VIEWER)).status_code == 403
            assert (await client.get('/api/audit')).status_code == 401
            creds = {'username': 'new-user', 'password': 'private-password-should-never-appear'}
            created = await client.post('/api/users', headers=ADMIN, json=creds)
            assert created.status_code == 201
            uid = created.json()['id']
            assert (await client.put(f'/api/users/{uid}/access', headers=ADMIN,
                                     json={'source_ids': [1], 'region': '华东'})).status_code == 200
            assert (await client.post('/api/sources/import', headers=ADMIN,
                                      json={'name': 'bad', 'csv_text': 'confidential-csv'})).status_code == 422
            invalid = await client.post('/api/previews', json={**QUERY, 'metric': creds['password']}, headers=VIEWER)
            assert invalid.status_code == 422
            malformed = await client.post('/api/previews', headers={**VIEWER, 'Content-Type': 'application/json'},
                                          content=b'{"metric":')
            assert malformed.status_code == 422
            preview = (await client.post('/api/previews', headers=VIEWER, json=QUERY)).json()
            with monkeypatch.context() as patch:
                patch.setattr(main, 'aggregate', fail)
                broken = await client.post(f"/api/previews/{preview['id']}/execute", headers=VIEWER)
                assert broken.status_code == 500
            assert (await client.post(f"/api/previews/{preview['id']}/execute", headers=VIEWER)).status_code == 200
            assert (await client.post(f"/api/previews/{preview['id']}/execute", headers=VIEWER)).status_code == 409

            audit = await client.get('/api/audit', headers=ADMIN)
            assert audit.status_code == 200
            events = audit.json()['items']
            actions = {(row['action'], row['actor_id'], row['outcome'], row['status_code']) for row in events}
            assert ('user.create', 1, 'success', 201) in actions
            assert ('user.access', 1, 'success', 200) in actions
            assert ('source.import', 1, 'failure', 422) in actions
            assert sum(row['action'] == 'query.preview' and row['actor_id'] == 2 and
                       row['status_code'] == 422 for row in events) == 2
            assert ('query.execute', 2, 'failure', 500) in actions
            assert ('query.execute', 2, 'success', 200) in actions
            assert ('query.execute', 2, 'failure', 409) in actions
            assert all(row['duration_ms'] >= 0 for row in events)
            for secret in [creds['password'], 'admin-token', 'viewer-token', 'confidential-csv',
                           'internal-sensitive-database-error']:
                assert secret not in audit.text
            first = (await client.get('/api/audit?limit=1', headers=ADMIN)).json()
            second = (await client.get('/api/audit', headers=ADMIN,
                                       params={'before_id': first['next_cursor'], 'limit': 1})).json()
            assert first['items'][0]['id'] > second['items'][0]['id']
    asyncio.run(run())


def test_cleanup_only_expired(database):
    from app.cleanup import cleanup_expired

    now = datetime.utcnow()
    with database.begin() as db:
        for key, seconds in [('old', -1), ('boundary', 0), ('valid', 60)]:
            expires = now + timedelta(seconds=seconds)
            db.add(main.Preview(id=key, user_id=1, payload='{}', source_version=1, expires=expires))
            db.add(main.Token(user_id=1, digest=key, expires=expires))
    assert cleanup_expired(database, now) == {'previews': 2, 'sessions': 2}
    assert cleanup_expired(database, now) == {'previews': 0, 'sessions': 0}
    with database() as db:
        assert db.get(main.Preview, 'valid') is not None
        assert db.scalar(select(func.count()).select_from(main.Token)) == 3
