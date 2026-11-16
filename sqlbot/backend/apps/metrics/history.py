"""Owner-scoped traces. Stored results never bypass current data permissions."""
from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select

from apps.metrics.models import MetricQuery, now_utc
from apps.metrics.schemas import MetricBinding, MetricIntent


def owned(session, user, query_id, lock=False):
    statement = select(MetricQuery).where(MetricQuery.id == query_id,
        MetricQuery.oid == user.oid, MetricQuery.user_id == user.id)
    if lock:
        statement = statement.with_for_update()
    row = session.exec(statement).first()
    if row is None:
        raise HTTPException(404, '查询记录不存在')
    return row


def event(row, stage, status, duration_ms, message=None):
    entry = dict(stage=stage, status=status, at=now_utc().isoformat() + 'Z',
                 duration_ms=max(0, round(duration_ms, 2)))
    if message:
        entry['message'] = message
    row.events = [*(row.events or []), entry]


def fail(session, row, stage, code, message, duration_ms):
    row.status = 'failed'
    row.error = dict(stage=stage, code=f'HTTP_{code}', message=message)
    event(row, stage, 'failed', duration_ms, message)
    session.add(row)
    session.commit()


def start_resolution(session, user, datasource_id, question):
    from apps.metrics.service import datasource_for_user
    datasource_for_user(session, user, datasource_id)
    row = MetricQuery(oid=user.oid, user_id=user.id, datasource_id=datasource_id,
                      plan={}, status='resolving', question=question, events=[])
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def finish_resolution(session, query_id, result, duration_ms):
    row = session.get(MetricQuery, query_id)
    row.resolution = result
    row.status = result['status']
    event(row, 'resolve', 'needs_clarification' if row.status == 'needs_clarification' else 'success', duration_ms)
    session.add(row)
    session.commit()


def summary(row):
    return dict(id=row.id, created_at=row.created_at.isoformat() + 'Z', status=row.status,
        question=row.question, metric_code=(row.plan or {}).get('intent', {}).get('metric_code'),
        version=(row.plan or {}).get('version'), origin='natural_language' if row.question is not None else 'manual',
        duration_ms=round(sum(item.get('duration_ms', 0) for item in (row.events or [])), 2))


def listing(session, user, datasource_id, page=1, page_size=20):
    from apps.metrics.service import datasource_for_user
    datasource_for_user(session, user, datasource_id)
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(422, '分页参数无效')
    where = (MetricQuery.oid == user.oid, MetricQuery.user_id == user.id,
             MetricQuery.datasource_id == datasource_id)
    total = session.exec(select(func.count()).select_from(MetricQuery).where(*where)).one()
    rows = session.exec(select(MetricQuery).where(*where).order_by(MetricQuery.created_at.desc(), MetricQuery.id.desc())
                        .offset((page - 1) * page_size).limit(page_size)).all()
    return dict(items=[summary(row) for row in rows], total=total, page=page, page_size=page_size)


def detail(session, user, query_id):
    from apps.metrics.service import datasource_for_user, authorized_context, datasource_fingerprint
    from apps.metrics.compiler import compile_metric
    row = owned(session, user, query_id)
    datasource_for_user(session, user, row.datasource_id)
    plan = row.plan or {}
    query = None
    if plan:
        try:
            binding = MetricBinding.model_validate(plan['binding'])
            intent = MetricIntent.model_validate(plan['intent'])
            ds, schema, filters = authorized_context(session, user, row.datasource_id, binding, intent)
            compiled = compile_metric(binding, intent, schema, filters)
            if (compiled.sql != plan['sql'] or compiled.params != plan['parameters']
                    or datasource_fingerprint(ds) != plan.get('datasource_fingerprint')):
                raise ValueError('Access context changed')
        except (HTTPException, ValueError, KeyError, TypeError):
            raise HTTPException(403, '当前权限或数据源配置已变化，无法查看这条历史查询的详情') from None
        # Internal fingerprints are unnecessary in a history response.
        visible_plan = {key: value for key, value in plan.items() if key != 'datasource_fingerprint'}
        query = dict(id=row.id, status=row.status, **visible_plan, **(row.result or {}))
    return dict(**summary(row), resolution=row.resolution, events=row.events or [], error=row.error, query=query)
