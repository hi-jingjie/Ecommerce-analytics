import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app import main


@pytest.fixture(params=['sqlite', 'postgresql'])
def isolated_engine(request, tmp_path, monkeypatch):
    admin_engine = None
    if request.param == 'sqlite':
        engine = create_engine(f'sqlite:///{tmp_path}/analytics.db')
    else:
        url = os.getenv('TEST_POSTGRES_URL')
        if not url:
            pytest.skip('Set TEST_POSTGRES_URL to run the PostgreSQL tests')
        schema = 'analytics_test_' + uuid4().hex
        admin_engine = create_engine(url)
        with admin_engine.begin() as conn:
            conn.execute(CreateSchema(schema))
        engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        monkeypatch.setattr(main, 'engine', engine)
        monkeypatch.setattr(main, 'SessionLocal', sessionmaker(bind=engine))
        yield engine
    finally:
        engine.dispose()
        if admin_engine is not None:
            with admin_engine.begin() as conn:
                conn.execute(DropSchema(schema, cascade=True))
            admin_engine.dispose()
