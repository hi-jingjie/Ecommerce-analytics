"""Authorization, immutable definitions, preview/confirm and read-only execution."""
import json
import hashlib
import time
from datetime import timedelta
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from apps.metrics.compiler import compile_metric, required_fields
from apps.metrics.models import MetricDefinition, MetricQuery, now_utc
from apps.metrics.schemas import CATALOG, MetricBinding, MetricIntent, PublishDefinition


def require_editor(user):
    if not user.isAdmin and user.weight <= 0:
        raise HTTPException(403, '只有工作空间管理员可以发布指标口径')


def datasource_fingerprint(ds):
    payload = [ds.id, getattr(ds, 'type', ''), getattr(ds, 'configuration', '')]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def datasource_for_user(session, user, datasource_id):
    if not user.isAdmin and user.weight < 0:
        raise HTTPException(403, '当前账号不是此工作空间的成员')
    from apps.datasource.models.datasource import CoreDatasource
    ds = session.get(CoreDatasource, datasource_id)
    if not ds or ds.oid != user.oid:
        raise HTTPException(404, '数据源不存在或不属于当前工作空间')
    if ds.type != 'pg':
        raise HTTPException(422, '指标工具首版仅支持 PostgreSQL 数据源')
    return ds


def source_metadata(session, user, datasource_id):
    from apps.datasource.crud.datasource import get_table_obj_by_ds
    ds = datasource_for_user(session, user, datasource_id)
    tables = get_table_obj_by_ds(session, user, ds)
    return [{'name': item.table.table_name,
             'fields': [{'name': field.field_name, 'type': field.field_type}
                        for field in (item.fields or [])]}
            for item in tables if item.fields]


def authorized_context(session, user, datasource_id, binding, intent=None):
    from apps.datasource.crud.datasource import get_table_obj_by_ds
    from apps.datasource.crud.permission import get_row_permission_filters
    from apps.datasource.models.datasource import DatasourceConf
    from apps.datasource.utils.utils import aes_decrypt
    from apps.metrics.permissions import strict_row_filter

    ds = datasource_for_user(session, user, datasource_id)
    tables = get_table_obj_by_ds(session, user, ds)
    table = next((item for item in tables if item.table.table_name == binding.table_name), None)
    if table is None:
        raise HTTPException(403, '指标表未启用或不可访问')
    if intent is None:
        fields = {binding.grain_field, binding.time_field, binding.status_field, *binding.dimensions.values()}
        if binding.amount_field:
            fields.add(binding.amount_field)
    else:
        fields = required_fields(binding, intent)
    available = {field.field_name: field for field in (table.fields or [])}
    if not fields.issubset(available):
        raise HTTPException(403, '指标依赖的字段未启用或没有访问权限')
    time_type = (available[binding.time_field].field_type or '').lower()
    if not any(kind in time_type for kind in ('timestamp', 'date')):
        raise HTTPException(422, '时间字段必须是 PostgreSQL date 或 timestamp 类型')
    if binding.metric_code != 'paid_orders':
        amount_type = (available[binding.amount_field].field_type or '').lower()
        if not any(kind in amount_type for kind in ('numeric', 'decimal', 'int', 'float', 'double', 'real')):
            raise HTTPException(422, '金额字段必须是数值类型，单位为元')
    permissions = get_row_permission_filters(session=session, current_user=user, ds=ds,
        single_table=table.table,
        filter_renderer=lambda s, u, rules, source: strict_row_filter(s, u, rules, source, table.table.id))
    row_filter = ' AND '.join(f'({item["filter"]})' for item in permissions if item.get('filter'))
    conf = DatasourceConf(**json.loads(aes_decrypt(ds.configuration)))
    return ds, conf.dbSchema or 'public', row_filter


def latest(session: Session, oid: int, datasource_id: int, metric_code: str):
    return session.exec(select(MetricDefinition).where(
        MetricDefinition.oid == oid, MetricDefinition.datasource_id == datasource_id,
        MetricDefinition.metric_code == metric_code).order_by(MetricDefinition.version.desc())).first()


def list_definitions(session, user, datasource_id):
    datasource_for_user(session, user, datasource_id)
    rows = session.exec(select(MetricDefinition).where(
        MetricDefinition.oid == user.oid, MetricDefinition.datasource_id == datasource_id
    ).order_by(MetricDefinition.metric_code, MetricDefinition.version.desc())).all()
    available = {table['name']: {field['name'] for field in table['fields']}
                 for table in source_metadata(session, user, datasource_id)}
    visible = []
    seen = set()
    blocked = set()
    for row in rows:
        if row.metric_code in blocked:
            continue
        is_latest = row.metric_code not in seen
        seen.add(row.metric_code)
        binding = MetricBinding.model_validate(row.binding)
        allowed = available.get(binding.table_name, set())
        binding.dimensions = {key: value for key, value in binding.dimensions.items() if value in allowed}
        try:
            authorized_context(session, user, datasource_id, binding)
        except HTTPException as error:
            if error.status_code == 403:
                if is_latest:
                    blocked.add(row.metric_code)
                continue
            raise
        visible.append(row.model_copy(update={'binding': binding.model_dump(mode='json')}))
    return visible


def publish(session: Session, user, request: PublishDefinition):
    require_editor(user)
    authorized_context(session, user, request.datasource_id, request.binding)
    current = latest(session, user.oid, request.datasource_id, request.binding.metric_code)
    version = current.version if current else 0
    if request.expected_version != version:
        raise HTTPException(409, '口径已更新，请刷新后重新编辑')
    definition = MetricDefinition(oid=user.oid, datasource_id=request.datasource_id,
        metric_code=request.binding.metric_code, version=version + 1,
        binding=request.binding.model_dump(mode='json'), created_by=user.id)
    session.add(definition)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, '其他用户刚刚发布了新版本，请刷新后重试') from None
    session.refresh(definition)
    return definition


def preview(session: Session, user, datasource_id: int, intent: MetricIntent, resolution_id=None):
    from apps.metrics import history
    datasource_for_user(session, user, datasource_id)
    started = time.monotonic()
    if resolution_id:
        row = history.owned(session, user, resolution_id, lock=True)
        if (row.datasource_id != datasource_id or row.status != 'ready'
                or now_utc() - row.created_at > timedelta(minutes=30)
                or not row.resolution or row.resolution.get('intent') != intent.model_dump(mode='json')):
            raise HTTPException(409, '解析记录已失效或参数发生变化，请重新解析')
    else:
        row = MetricQuery(oid=user.oid, user_id=user.id, datasource_id=datasource_id, plan={}, events=[])
        session.add(row)
        session.commit()
        session.refresh(row)
    query_id = row.id
    original_status, original_plan, original_events = row.status, row.plan, row.events
    try:
        return _preview(session, user, datasource_id, intent, row, started)
    except Exception as error:
        session.rollback()
        # Rollback releases the lock: another request may have advanced this
        # resolution while we were failing. Re-lock and preserve its progress.
        row = history.owned(session, user, query_id, lock=True)
        code = error.status_code if isinstance(error, HTTPException) else (422 if isinstance(error, ValueError) else 500)
        message = ('查询预览失败，请检查口径、字段权限及参数' if code != 500 else '查询预览失败，请稍后重试')
        if row.status == original_status and row.plan == original_plan and row.events == original_events:
            history.fail(session, row, 'preview', code, message, (time.monotonic() - started) * 1000)
        raise HTTPException(code, message) from None


def _preview(session, user, datasource_id, intent, row, started):
    from apps.metrics import history
    definition = latest(session, user.oid, datasource_id, intent.metric_code)
    if definition is None:
        raise HTTPException(422, '该数据源尚未配置此指标，请先发布口径')
    binding = MetricBinding.model_validate(definition.binding)
    ds, schema, row_filter = authorized_context(session, user, datasource_id, binding, intent)
    compiled = compile_metric(binding, intent, schema, row_filter)
    shown_binding = binding.model_dump(mode='json')
    shown_binding['dimensions'] = {key: value for key, value in binding.dimensions.items()
                                   if key == intent.dimension or key in intent.filters}
    plan = dict(intent=intent.model_dump(mode='json'), binding=shown_binding,
                metric=CATALOG[intent.metric_code], version=definition.version,
                sql=compiled.sql, parameters=compiled.params, datasource_fingerprint=datasource_fingerprint(ds))
    row.definition_id = definition.id
    row.plan = plan
    row.status = 'preview'
    history.event(row, 'preview', 'success', (time.monotonic() - started) * 1000)
    session.add(row)
    session.commit()
    session.refresh(row)
    return dict(id=row.id, status=row.status, **plan)


def run_query(ds, binding, compiled):
    from apps.db.db import get_engine
    engine = get_engine(ds, timeout=30)
    try:
        with engine.connect() as connection:
            with connection.begin():
                # Integrity check and aggregation share one snapshot.
                connection.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                connection.execute(text("SET LOCAL statement_timeout = '30s'"))
                connection.execute(text("SET LOCAL TIME ZONE 'Asia/Shanghai'"))
                connection.execute(text('SET LOCAL standard_conforming_strings = on'))
                check = connection.execute(text(compiled.integrity_sql), compiled.params).mappings().one()
                if check['row_count'] != check['grain_count']:
                    raise HTTPException(422, '订单/退款流水编号存在空值或重复，请绑定每笔业务一行的表或视图')
                if check['null_amount_count']:
                    raise HTTPException(422, '统计范围存在空金额，请修复数据后重试')
                rows = connection.execute(text(compiled.sql), compiled.params).mappings().all()
                if len(rows) > 1000:
                    raise HTTPException(422, '分组超过 1000 个，请缩小日期或添加筛选条件')
                return [{key: format(value, 'f') if isinstance(value, Decimal) else value
                         for key, value in row.items()} for row in rows]
    except SQLAlchemyError:
        # Driver errors may contain connection info or SQL values; don't expose them.
        raise HTTPException(422, '数据源查询失败或超时，请检查连接、字段类型、字段映射和数据库权限') from None
    finally:
        engine.dispose()


def execute(session: Session, user, query_id: str):
    from apps.metrics import history
    row = history.owned(session, user, query_id, lock=True)
    # Repeat calls must not overwrite completed records or append misleading failures.
    if row.status != 'preview':
        raise HTTPException(409, '计划已执行或不可执行，请重新预览')
    started = time.monotonic()
    try:
        return _execute(session, user, query_id, started)
    except Exception as error:
        session.rollback()
        row = history.owned(session, user, query_id, lock=True)
        code = error.status_code if isinstance(error, HTTPException) else 500
        message = {403: '当前权限不足，无法执行查询', 404: '查询依赖的数据源或口径不存在',
                   409: '计划已过期，或口径、权限、数据源已变化，请重新预览',
                   422: '数据源查询失败，请检查数据完整性、字段配置和连接状态'}.get(code, '查询执行失败，请稍后重试')
        if row.status == 'preview':
            history.fail(session, row, 'execute', code, message, (time.monotonic() - started) * 1000)
        raise HTTPException(code, message) from None


def _execute(session: Session, user, query_id: str, started):
    from apps.metrics import history
    row = session.exec(select(MetricQuery).where(MetricQuery.id == query_id,
        MetricQuery.oid == user.oid, MetricQuery.user_id == user.id).with_for_update()).first()
    if row is None:
        raise HTTPException(404, '查询计划不存在')
    if row.status != 'preview' or now_utc() - row.created_at > timedelta(minutes=30):
        raise HTTPException(409, '计划已执行或过期，请重新预览')
    definition = session.get(MetricDefinition, row.definition_id)
    current = latest(session, user.oid, row.datasource_id, definition.metric_code) if definition else None
    if current is None or current.id != row.definition_id:
        raise HTTPException(409, '指标口径已改变，请按最新口径重新预览并确认')
    binding = MetricBinding.model_validate(definition.binding)
    intent = MetricIntent.model_validate(row.plan['intent'])
    ds, schema, row_filter = authorized_context(session, user, row.datasource_id, binding, intent)
    compiled = compile_metric(binding, intent, schema, row_filter)
    # Rechecking permissions must not silently alter a plan the user approved.
    if (compiled.sql != row.plan['sql'] or compiled.params != row.plan['parameters']
            or datasource_fingerprint(ds) != row.plan.get('datasource_fingerprint')):
        raise HTTPException(409, '权限或数据源设置已改变，请重新预览并确认')
    rows = run_query(ds, binding, compiled)
    row.status = 'completed'
    row.result = dict(rows=rows, executed_at=now_utc().isoformat() + 'Z')
    history.event(row, 'execute', 'success', (time.monotonic() - started) * 1000)
    session.add(row)
    session.commit()
    return dict(id=row.id, status=row.status, **row.plan, **row.result)
