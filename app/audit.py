"""Execution history and transaction-aware audit records."""
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timezone

from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from .models import AuditEvent


ROUTE_ACTIONS = {
    'setup': 'system.setup',
    'demo': 'source.demo',
    'import_csv': 'source.import',
    'reconcile_refunds': 'refund.reconcile',
    'create_user': 'user.create',
    'update_access': 'user.access',
    'preview': 'query.preview',
    'execute_preview': 'query.execute',
}
# Only these constant application messages may enter the ledger. Validation
# errors, parser exceptions, SQL errors and request bodies can contain secrets.
SAFE_REASONS = {
    '请登录', '登录已失效', '需要管理员权限', '需要服务端配置的初始化令牌',
    '初始化已完成', '初始化已由其他请求完成', '已有数据', '数据源名称已存在',
    '数据源不存在', '用户不存在', '用户名已存在', '不能通过此接口修改管理员',
    '无数据源访问权限', '日期范围无效或超过十年', '预览不存在',
    '预览已执行或过期，请重新预览', '预览已执行', '权限或数据版本已变化，请重新预览',
}
log = logging.getLogger(__name__)


@dataclass
class Attempt:
    started: float = field(default_factory=time.monotonic)
    actor_id: int | None = None
    details: dict = field(default_factory=dict)


# FastAPI copies context into synchronous route/dependency worker threads.
# Each request shares its own Attempt across those copies.
_attempt: ContextVar[Attempt | None] = ContextVar('audit_attempt', default=None)


def identify_actor(actor_id):
    attempt = _attempt.get()
    if attempt is not None:
        attempt.actor_id = actor_id


def add_context(**details):
    attempt = _attempt.get()
    if attempt is not None:
        attempt.details.update(details)


def elapsed_ms(started):
    return int((time.monotonic() - started) * 1000)


def write(db, action, actor_id, outcome, status_code, details):
    attempt = _attempt.get()
    db.add(AuditEvent(
        actor_id=actor_id, action=action, outcome=outcome, status_code=status_code,
        details=json.dumps(details, ensure_ascii=False),
        duration_ms=elapsed_ms(attempt.started) if attempt else 0,
    ))


def success(db, action, actor_id, status_code=200, **details):
    """Attach to the same transaction as the successful operation."""
    write(db, action, actor_id, 'success', status_code, details)


def audited_routes(session_factory, session_actor):
    class AuditedRoute(APIRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()
            action = ROUTE_ACTIONS.get(self.name)
            if action is None:
                return handler

            async def handle(request):
                attempt = Attempt()
                token = _attempt.set(attempt)
                try:
                    return await handler(request)
                except Exception as exc:
                    status, reason = 500, '内部错误'
                    if isinstance(exc, RequestValidationError):
                        status, reason = 422, '请求参数无效'
                    elif isinstance(exc, HTTPException):
                        status = exc.status_code
                        if isinstance(exc.detail, str) and exc.detail in SAFE_REASONS:
                            reason = exc.detail
                        elif action == 'source.import' and status == 422:
                            reason = 'CSV 数据无效'
                        else:
                            reason = '操作被拒绝'

                    def save_failure():
                        # The endpoint's transaction has unwound at this point.
                        with session_factory() as db:
                            # A malformed JSON body may fail before authentication dependencies run.
                            actor_id = attempt.actor_id
                            if actor_id is None and status in (400, 422) and action != 'system.setup':
                                actor = session_actor(db, request.headers.get('authorization'))
                                actor_id = actor.id if actor is not None else None
                            write(db, action, actor_id, 'failure', status, {**attempt.details, 'reason': reason})
                            db.commit()

                    try:
                        await run_in_threadpool(save_failure)
                    except Exception:
                        # Preserve the original API error during a database outage.
                        log.error('Could not persist audit failure for %s (HTTP %s)', action, status)
                    raise
                finally:
                    _attempt.reset(token)

            return handle

    return AuditedRoute


def date_basis(metric):
    return '退款发生日' if metric == 'refund_amount' else '支付日'


def history_item(row):
    return {
        'id': row.id, 'created_at': row.created_at.replace(tzinfo=timezone.utc), 'preview_id': row.preview_id,
        'metric': row.metric, 'start_date': row.start_date, 'end_date': row.end_date,
        'source_id': row.source_id, 'source_name': row.source_name, 'source_version': row.source_version,
        'channel': row.channel, 'region': row.region, 'definition_version': row.definition_version,
        'date_basis': date_basis(row.metric), 'value': str(row.value),
        'matched_count': row.matched_count, 'duration_ms': row.duration_ms,
    }


def audit_item(row):
    return {
        'id': row.id, 'created_at': row.created_at.replace(tzinfo=timezone.utc), 'actor_id': row.actor_id,
        'action': row.action, 'outcome': row.outcome, 'status_code': row.status_code,
        'duration_ms': row.duration_ms, 'details': json.loads(row.details),
    }


def page(db, stmt, id_column, serialize, limit, before_id):
    if before_id is not None:
        stmt = stmt.where(id_column < before_id)
    rows = db.scalars(stmt.order_by(id_column.desc()).limit(limit + 1)).all()
    return {
        'items': [serialize(row) for row in rows[:limit]],
        'next_cursor': rows[limit - 1].id if len(rows) > limit else None,
    }
