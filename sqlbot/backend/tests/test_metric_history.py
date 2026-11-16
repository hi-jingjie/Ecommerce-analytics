import importlib.util
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from apps.metrics import service
from apps.metrics.models import MetricDefinition, MetricQuery
from apps.metrics.schemas import MetricBinding, MetricIntent, PublishDefinition


def history():
    assert importlib.util.find_spec('apps.metrics.history'), 'history module missing'
    from apps.metrics import history
    return history


def user(id=1, oid=1):
    return SimpleNamespace(id=id, oid=oid, isAdmin=True, weight=1)


@pytest.fixture
def session(monkeypatch):
    engine = create_engine('sqlite://')
    SQLModel.metadata.create_all(engine, tables=[MetricDefinition.__table__, MetricQuery.__table__])
    monkeypatch.setattr(service, 'datasource_for_user', lambda *args: SimpleNamespace(id=1))
    monkeypatch.setattr(service, 'authorized_context', lambda *args: (SimpleNamespace(id=1), 'public', ''))
    with Session(engine) as session:
        binding = MetricBinding(metric_code='payment_amount', table_name='orders', grain_field='id',
            amount_field='amount', time_field='paid_at', status_field='status', status_values=['paid'])
        service.publish(session, user(), PublishDefinition(datasource_id=1, expected_version=0, binding=binding))
        yield session
    engine.dispose()


def intent():
    return MetricIntent(metric_code='payment_amount', start_date='2026-10-19', end_date='2026-10-25')


def ready(session):
    h = history()
    row = h.start_resolution(session, user(), 1, '这周的支付金额')
    h.finish_resolution(session, row.id, {'status': 'ready', 'intent': intent().model_dump(mode='json'),
                                         'questions': [], 'model_id': 1, 'usage': None}, 12)
    return row


def test_resolution_preview_and_execution_share_history(session, monkeypatch):
    row = ready(session)
    plan = service.preview(session, user(), 1, intent(), resolution_id=row.id)
    assert plan['id'] == row.id
    monkeypatch.setattr(service, 'run_query', lambda *args: [{'value': '351'}])
    service.execute(session, user(), row.id)
    detail = history().detail(session, user(), row.id)
    assert detail['query']['rows'] == [{'value': '351'}]
    assert [event['stage'] for event in detail['events']] == ['resolve', 'preview', 'execute']
    assert detail['origin'] == 'natural_language'
    assert detail['duration_ms'] >= 12


@pytest.mark.parametrize('other', [user(id=2), user(oid=2)])
def test_history_and_resolution_cannot_cross_identity(session, other):
    row = ready(session)
    with pytest.raises(HTTPException) as error:
        history().detail(session, other, row.id)
    assert error.value.status_code == 404
    assert history().listing(session, other, 1, 1, 20)['total'] == 0
    with pytest.raises(HTTPException) as error:
        service.preview(session, other, 1, intent(), resolution_id=row.id)
    assert error.value.status_code == 404
    assert session.get(MetricQuery, row.id).status == 'ready'


def test_changed_intent_is_rejected_without_changing_resolution(session):
    row = ready(session)
    changed = intent().model_copy(update={'filters': {'channel': 'Web'}})
    with pytest.raises(HTTPException) as error:
        service.preview(session, user(), 1, changed, resolution_id=row.id)
    assert error.value.status_code == 409
    assert session.get(MetricQuery, row.id).status == 'ready'


def test_failed_preview_does_not_overwrite_concurrently_completed_query(session, monkeypatch):
    row = ready(session)
    query_id = row.id
    original_preview = service._preview
    first = True
    def fail_first(*args):
        nonlocal first
        if first:
            first = False
            raise HTTPException(422, 'temporary preview failure')
        return original_preview(*args)
    monkeypatch.setattr(service, '_preview', fail_first)
    monkeypatch.setattr(service, 'run_query', lambda *args: [{'value': '351'}])
    original_rollback = session.rollback
    def concurrent_success():
        original_rollback()
        # Deterministically exercise the window where PostgreSQL releases the
        # first request's lock and a second request finishes before it resumes.
        with Session(session.bind) as other:
            service.preview(other, user(), 1, intent(), resolution_id=query_id)
            service.execute(other, user(), query_id)
    monkeypatch.setattr(session, 'rollback', concurrent_success)
    with pytest.raises(HTTPException) as error:
        service.preview(session, user(), 1, intent(), resolution_id=query_id)
    assert error.value.status_code == 422
    session.expire_all()
    detail = history().detail(session, user(), query_id)
    assert detail['status'] == 'completed'
    assert detail['error'] is None
    assert detail['query']['rows'] == [{'value': '351'}]
    assert [event['stage'] for event in detail['events']] == ['resolve', 'preview', 'execute']


def test_completed_query_is_not_overwritten_by_repeat_execution(session, monkeypatch):
    history()
    plan = service.preview(session, user(), 1, intent())
    monkeypatch.setattr(service, 'run_query', lambda *args: [{'value': '351'}])
    service.execute(session, user(), plan['id'])
    with pytest.raises(HTTPException):
        service.execute(session, user(), plan['id'])
    detail = history().detail(session, user(), plan['id'])
    assert detail['status'] == 'completed' and detail['error'] is None
    assert len(detail['events']) == 2


def test_execution_failure_is_saved_without_raw_exception(session, monkeypatch):
    history()
    plan = service.preview(session, user(), 1, intent())
    def fail(*args):
        raise RuntimeError('password=secret-value connection info')
    monkeypatch.setattr(service, 'run_query', fail)
    with pytest.raises(HTTPException) as error:
        service.execute(session, user(), plan['id'])
    assert error.value.status_code == 500
    detail = history().detail(session, user(), plan['id'])
    assert detail['status'] == 'failed'
    assert detail['error']['stage'] == 'execute'
    assert 'secret-value' not in str(detail)


@pytest.mark.parametrize('change', ['field', 'rows', 'source'])
def test_history_rechecks_access_before_revealing_old_results(session, monkeypatch, change):
    history()
    plan = service.preview(session, user(), 1, intent())
    monkeypatch.setattr(service, 'run_query', lambda *args: [{'value': '351'}])
    service.execute(session, user(), plan['id'])
    def access(*args):
        if change == 'field':
            raise HTTPException(403, 'field revoked')
        return (SimpleNamespace(id=1, configuration='changed') if change == 'source' else SimpleNamespace(id=1),
                'public', 'region = 1' if change == 'rows' else '')
    monkeypatch.setattr(service, 'authorized_context', access)
    with pytest.raises(HTTPException) as error:
        history().detail(session, user(), plan['id'])
    assert error.value.status_code == 403


def test_legacy_history_and_pagination(session):
    h = history()
    plan = service.preview(session, user(), 1, intent())
    row = session.get(MetricQuery, plan['id'])
    row.events = None
    session.add(row)
    session.commit()
    ready(session)
    page = h.listing(session, user(), 1, 1, 1)
    assert page['total'] == 2 and len(page['items']) == 1
    assert 'plan' not in page['items'][0] and 'resolution' not in page['items'][0]
    assert h.detail(session, user(), row.id)['events'] == []
    assert h.detail(session, user(), row.id)['created_at'].endswith('Z')


def test_resolve_failure_is_recorded_and_has_no_query(session):
    h = history()
    row = h.start_resolution(session, user(), 1, '支付金额？')
    h.fail(session, row, 'resolve', 503, '模型解析不可用，请配置默认模型', 10)
    detail = h.detail(session, user(), row.id)
    assert detail['query'] is None and detail['error']['code'] == 'HTTP_503'
    assert detail['question'] == '支付金额？'


@pytest.mark.parametrize('mode', ['ready', 'needs_clarification', 'unavailable'])
def test_resolve_endpoint_persists_real_workflow_outcomes(session, monkeypatch, mode):
    import asyncio
    from apps.metrics import api
    from apps.metrics.schemas import ResolveRequest
    monkeypatch.setattr(service, 'list_definitions', lambda *args: [SimpleNamespace(metric_code='payment_amount')])
    async def resolve(*args):
        if mode == 'unavailable':
            raise RuntimeError('private-provider-credential')
        return {'status': mode, 'intent': intent().model_dump(mode='json') if mode == 'ready' else None,
                'questions': [] if mode == 'ready' else ['请明确时间'], 'model_id': 1, 'usage': None}
    monkeypatch.setattr(api, 'resolve_question', resolve)
    # SQLite sessions in this test stay on their owning thread; production uses
    # FastAPI's sequential threadpool boundary for sync database operations.
    async def inline(func, *args):
        return func(*args)
    monkeypatch.setattr(api, 'run_in_threadpool', inline)
    request = ResolveRequest(datasource_id=1, question='上周支付金额')
    if mode == 'unavailable':
        with pytest.raises(HTTPException) as error:
            asyncio.run(api.resolve(request, session, user()))
        assert error.value.status_code == 503
        assert 'private-provider-credential' not in str(error.value)
    else:
        result = asyncio.run(api.resolve(request, session, user()))
        assert result['query_id']
    rows = history().listing(session, user(), 1)['items']
    assert len(rows) == 1
    detail = history().detail(session, user(), rows[0]['id'])
    assert detail['status'] == ('failed' if mode == 'unavailable' else mode)
    assert 'private-provider-credential' not in str(detail)
