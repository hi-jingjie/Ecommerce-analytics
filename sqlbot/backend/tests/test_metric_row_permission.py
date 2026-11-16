from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlmodel import Session, create_engine


def test_enum_permission_uses_datasource_object_and_filters_rows():
    from apps.datasource.crud.row_permission import transTreeItem
    from apps.datasource.models.datasource import CoreField, CoreDatasource
    engine = create_engine('sqlite://')
    CoreField.__table__.create(engine)
    with Session(engine) as session:
        session.add(CoreField(id=1, ds_id=1, table_id=1, field_name='channel', field_type='text', checked=True))
        session.commit()
        predicate = transTreeItem(session, SimpleNamespace(id=2), dict(type='item', field_id=1,
            term='in', filter_type='enum', enum_value=['Web']), CoreDatasource(type='pg'))
        assert predicate == '("channel" IN (\'Web\'))'
    engine.dispose()


@pytest.mark.parametrize('tree', [None, {'logic': 'AND', 'items': []}, {'logic': 'XOR', 'items': []},
    {'logic': 'OR', 'items': [{'type': 'item', 'field_id': 1, 'filter_type': 'enum', 'term': 'in', 'enum_value': []}]},
    {'logic': 'AND', 'items': [{'type': 'item', 'field_id': 1, 'filter_type': 'enum', 'term': 'in', 'enum_value': ['Web']}]}])
def test_unresolvable_or_cross_table_row_permission_fails_closed(tree):
    from apps.metrics.permissions import strict_row_filter
    engine = create_engine('sqlite://')
    from apps.datasource.models.datasource import CoreField, CoreDatasource
    CoreField.__table__.create(engine)
    with Session(engine) as session:
        session.add(CoreField(id=1, ds_id=1, table_id=99, field_name='channel', field_type='text', checked=True))
        session.commit()
        with pytest.raises(HTTPException) as error:
            strict_row_filter(session, SimpleNamespace(id=2), [SimpleNamespace(tree=tree)], CoreDatasource(id=1, type='pg'), 1)
        assert error.value.status_code == 403
    engine.dispose()


def test_strict_permission_preserves_enum_exclusion_and_escapes_values():
    from apps.metrics.permissions import strict_row_filter
    from apps.datasource.models.datasource import CoreField, CoreDatasource
    engine = create_engine('sqlite://')
    CoreField.__table__.create(engine)
    with Session(engine) as session:
        session.add(CoreField(id=1, ds_id=1, table_id=1, field_name='channel', field_type='text', checked=True))
        session.commit()
        tree = {'logic': 'AND', 'items': [{'type': 'item', 'field_id': 1, 'filter_type': 'enum',
            'term': 'not in', 'enum_value': ["Web' OR 1=1 --"]}]}
        predicate = strict_row_filter(session, SimpleNamespace(id=2), [SimpleNamespace(tree=tree)], CoreDatasource(id=1, type='pg'), 1)
        assert predicate == '("channel" NOT IN (\'Web\'\' OR 1=1 --\'))'
    engine.dispose()
