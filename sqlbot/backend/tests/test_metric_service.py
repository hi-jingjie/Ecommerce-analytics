from datetime import timedelta
from types import SimpleNamespace
import importlib.util

import pytest
from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine

from apps.metrics.models import MetricDefinition, MetricQuery, now_utc
from apps.metrics.schemas import MetricBinding, MetricIntent, PublishDefinition


def test_service_exists():
    assert importlib.util.find_spec('apps.metrics.service') is not None, 'service is missing'


def test_removed_workspace_member_cannot_access_datasource():
    from apps.metrics.service import datasource_for_user
    former_member = user(admin=False)
    former_member.weight = -1
    with pytest.raises(HTTPException) as error:
        datasource_for_user(None, former_member, 1)
    assert error.value.status_code == 403


@pytest.fixture
def session():
    engine = create_engine('sqlite://')
    SQLModel.metadata.create_all(engine, tables=[MetricDefinition.__table__, MetricQuery.__table__])
    with Session(engine) as session:
        yield session
    engine.dispose()


def user(oid=1, id=1, admin=True):
    return SimpleNamespace(oid=oid, id=id, isAdmin=admin, weight=1 if admin else 0)


def definition():
    return MetricBinding(metric_code='payment_amount', table_name='orders', grain_field='id', amount_field='amount',
                         time_field='paid_at', status_field='status', status_values=['paid'], dimensions={})


@pytest.fixture
def authorized(monkeypatch):
    from apps.metrics import service
    # Only the external datasource metadata boundary is substituted. Persistence,
    # version checks, plan validation and query compilation stay real.
    monkeypatch.setattr(service, 'authorized_context', lambda *args, **kwargs:
                        (SimpleNamespace(id=1), 'public', ''))
    monkeypatch.setattr(service, 'datasource_for_user', lambda *args: SimpleNamespace(id=1))
    return service


def test_publication_versions_are_immutable_and_optimistic(session, authorized):
    req = PublishDefinition(datasource_id=1, expected_version=0, binding=definition())
    first = authorized.publish(session, user(), req)
    assert first.version == 1
    with pytest.raises(HTTPException) as error:
        authorized.publish(session, user(), req)
    assert error.value.status_code == 409
    req.expected_version = 1
    req.binding.status_values = ['paid', 'refunded']
    second = authorized.publish(session, user(), req)
    assert second.version == 2
    assert session.get(MetricDefinition, first.id).binding['status_values'] == ['paid']


def test_regular_user_cannot_publish(session, authorized):
    with pytest.raises(HTTPException) as error:
        authorized.publish(session, user(admin=False), PublishDefinition(datasource_id=1, expected_version=0, binding=definition()))
    assert error.value.status_code == 403


def test_definition_lookup_is_scoped_to_workspace(session, authorized):
    authorized.publish(session, user(), PublishDefinition(datasource_id=1, expected_version=0, binding=definition()))
    assert authorized.latest(session, 2, 1, 'payment_amount') is None


def test_preview_is_persisted_and_stale_definition_cannot_execute(session, authorized):
    authorized.publish(session, user(), PublishDefinition(datasource_id=1, expected_version=0, binding=definition()))
    intent = MetricIntent(metric_code='payment_amount', start_date='2026-10-19', end_date='2026-10-25')
    plan = authorized.preview(session, user(), 1, intent)
    assert plan['status'] == 'preview'
    assert session.get(MetricQuery, plan['id']).plan['intent']['start_date'] == '2026-10-19'
    authorized.publish(session, user(), PublishDefinition(datasource_id=1, expected_version=1, binding=definition()))
    with pytest.raises(HTTPException) as error:
        authorized.execute(session, user(), plan['id'])
    assert error.value.status_code == 409


@pytest.mark.parametrize('other', [user(id=2), user(oid=2)])
def test_confirmation_is_bound_to_owner_and_workspace(session, authorized, other):
    row = MetricQuery(oid=1, user_id=1, datasource_id=1, definition_id='missing', plan={})
    session.add(row)
    session.commit()
    with pytest.raises(HTTPException) as error:
        authorized.execute(session, other, row.id)
    assert error.value.status_code == 404


def test_expired_plan_requires_new_preview(session, authorized):
    row = MetricQuery(oid=1, user_id=1, datasource_id=1, definition_id='missing', plan={},
                      created_at=now_utc() - timedelta(hours=1))
    session.add(row)
    session.commit()
    with pytest.raises(HTTPException) as error:
        authorized.execute(session, user(), row.id)
    assert error.value.status_code == 409


def test_datasource_change_invalidates_confirmation(session, authorized, monkeypatch):
    authorized.publish(session, user(), PublishDefinition(datasource_id=1, expected_version=0, binding=definition()))
    intent = MetricIntent(metric_code='payment_amount', start_date='2026-10-19', end_date='2026-10-25')
    plan = authorized.preview(session, user(), 1, intent)
    monkeypatch.setattr(authorized, 'authorized_context', lambda *args, **kwargs:
                        (SimpleNamespace(id=1, configuration='different database'), 'public', ''))
    monkeypatch.setattr(authorized, 'run_query', lambda *args: [{'value': 999}])
    with pytest.raises(HTTPException) as error:
        authorized.execute(session, user(), plan['id'])
    assert error.value.status_code == 409


def test_optional_hidden_dimension_does_not_hide_metric(session, authorized, monkeypatch):
    value = definition()
    value.dimensions = {'channel': 'channel'}
    authorized.publish(session, user(), PublishDefinition(datasource_id=1, expected_version=0, binding=value))
    monkeypatch.setattr(authorized, 'datasource_for_user', lambda *args: SimpleNamespace(id=1))
    monkeypatch.setattr(authorized, 'source_metadata', lambda *args: [{'name': 'orders', 'fields': [
        {'name': name, 'type': 'text'} for name in ['id', 'amount', 'paid_at', 'status']]}])
    rows = authorized.list_definitions(session, user(admin=False), 1)
    assert len(rows) == 1
    assert rows[0].binding['dimensions'] == {}


def test_inaccessible_latest_does_not_present_old_version_as_current(session, authorized, monkeypatch):
    req = PublishDefinition(datasource_id=1, expected_version=0, binding=definition())
    authorized.publish(session, user(), req)
    req.expected_version = 1
    req.binding.amount_field = 'hidden_amount'
    authorized.publish(session, user(), req)
    monkeypatch.setattr(authorized, 'datasource_for_user', lambda *args: SimpleNamespace(id=1))
    monkeypatch.setattr(authorized, 'source_metadata', lambda *args: [{'name': 'orders', 'fields': [
        {'name': name, 'type': 'text'} for name in ['id', 'amount', 'paid_at', 'status']]}])
    def access(session, user, source, binding, intent=None):
        if binding.amount_field == 'hidden_amount':
            raise HTTPException(403, 'hidden')
        return SimpleNamespace(id=1), 'public', ''
    monkeypatch.setattr(authorized, 'authorized_context', access)
    assert authorized.list_definitions(session, user(admin=False), 1) == []
