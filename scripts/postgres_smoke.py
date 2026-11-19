"""Check PostgreSQL using an isolated cluster, private socket, and synthetic data."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import sys
import asyncio

project = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--suite', action='store_true', help='also run pytest with PostgreSQL cases enabled')
args = parser.parse_args()
bin_path = Path(os.environ['PG_BIN'])
with tempfile.TemporaryDirectory(prefix='analytics-pg-') as folder:
    root = Path(folder)
    data = root / 'data'
    subprocess.run([str(bin_path/'initdb'), '-D', str(data), '-A', 'trust', '-U', 'demo', '--no-locale', '-E', 'UTF8'], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([str(bin_path/'pg_ctl'), '-D', str(data), '-l', str(root/'server.log'), '-o', f"-k {root} -h ''", '-w', 'start'], check=True, stdout=subprocess.DEVNULL)
    try:
        os.environ['DATABASE_URL'] = f'postgresql+psycopg://demo@/postgres?host={root}'
        os.environ['SETUP_TOKEN'] = 'pg-test-setup'
        from app import main
        import httpx
        main.init_db()
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://test') as c:
                creds={'username':'admin','password':'postgres-test-password'}
                r=await c.post('/api/setup',json=creds,headers={'X-Setup-Token':'pg-test-setup'})
                assert r.status_code==201,r.text
                token=(await c.post('/api/login',json=creds)).json()['access_token']
                headers={'Authorization':'Bearer '+token}
                assert (await c.post('/api/demo',headers=headers)).status_code==200
                r=await c.post('/api/users',headers=headers,json={'username':'reader','password':'postgres-test-reader','source_ids':[1]})
                assert r.status_code==201,r.text
                params={'source_id':1,'metric':'payment_amount','start_date':'2026-01-01','end_date':'2026-01-31'}
                preview=(await c.post('/api/previews',headers=headers,json=params)).json()
                result=await c.post('/api/previews/'+preview['id']+'/execute',headers=headers)
                assert result.json()['value']=='300.00',result.text
                assert len((await c.get('/api/history',headers=headers)).json())==1
        asyncio.run(check())
        main.engine.dispose()
        print('PASS PostgreSQL: schema, setup, login, demo, user creation, preview, aggregate, history')
        if args.suite:
            subprocess.run(
                [sys.executable, '-m', 'pytest', '-q'],
                env={**os.environ, 'TEST_POSTGRES_URL': os.environ['DATABASE_URL']},
                cwd=project, check=True,
            )
    finally:
        subprocess.run([str(bin_path/'pg_ctl'), '-D', str(data), '-m', 'fast', '-w', 'stop'], check=True, stdout=subprocess.DEVNULL)
