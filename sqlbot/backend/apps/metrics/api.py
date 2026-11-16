import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.concurrency import run_in_threadpool
from sqlmodel import select

from apps.metrics import service, history
from apps.metrics.agent import resolve_question
from apps.metrics.schemas import CATALOG, ConfirmRequest, MetricIntent, PreviewRequest, PublishDefinition, ResolveRequest
from common.core.deps import CurrentUser, SessionDep


def interactive_user(request: Request):
    if getattr(request.state, 'current_user', None) is None:
        raise HTTPException(401, '请先登录')
    if getattr(request.state, 'assistant', None) is not None:
        raise HTTPException(403, '指标工具暂不支持嵌入式助手身份，请使用用户登录')
    user = request.state.current_user
    if not user.isAdmin and user.weight < 0:
        raise HTTPException(403, '当前账号不是此工作空间的成员')


router = APIRouter(prefix='/metrics', tags=['Metrics'], dependencies=[Depends(interactive_user)])


@router.get('/catalog')
def catalog(current_user: CurrentUser):
    return dict(metrics=[dict(code=code, **value) for code, value in CATALOG.items()],
                timezone='Asia/Shanghai', can_manage=current_user.isAdmin or current_user.weight > 0)


@router.get('/tool-schema')
def tool_schema(current_user: CurrentUser):
    return {'name': 'preview_metric_query', 'description': '按已发布口径预览指标查询；执行须用户确认。',
            'parameters': PreviewRequest.model_json_schema()}


@router.get('/sources')
def sources(session: SessionDep, current_user: CurrentUser):
    from apps.datasource.models.datasource import CoreDatasource
    rows = session.exec(select(CoreDatasource).where(CoreDatasource.oid == current_user.oid,
                                                    CoreDatasource.type == 'pg')).all()
    return [{'id': row.id, 'name': row.name} for row in rows]


@router.get('/sources/{datasource_id}/metadata')
def metadata(datasource_id: int, session: SessionDep, current_user: CurrentUser):
    return service.source_metadata(session, current_user, datasource_id)


@router.get('/definitions')
def definitions(datasource_id: int, session: SessionDep, current_user: CurrentUser):
    return service.list_definitions(session, current_user, datasource_id)


@router.post('/definitions')
def publish(info: PublishDefinition, session: SessionDep, current_user: CurrentUser):
    return service.publish(session, current_user, info)


@router.post('/resolve')
async def resolve(info: ResolveRequest, session: SessionDep, current_user: CurrentUser):
    row = await run_in_threadpool(history.start_resolution, session, current_user, info.datasource_id, info.question)
    started = time.monotonic()
    try:
        definitions = await run_in_threadpool(service.list_definitions, session, current_user, info.datasource_id)
        available = sorted({row.metric_code for row in definitions})
        if not available:
            raise HTTPException(422, '请先为该数据源发布指标口径')
        result = await resolve_question(info.question, datetime.now(ZoneInfo('Asia/Shanghai')).date(), available)
    except (ValueError, asyncio.TimeoutError):
        code, message = 422, '模型输出未通过参数校验或请求超时，请补充问题或使用手动参数'
    except HTTPException as error:
        code, message = error.status_code, '指标解析不可用，请检查数据源权限及已发布口径'
    except Exception:
        code, message = 503, '模型解析不可用，请在模型管理中配置默认聊天模型，或使用手动参数'
    else:
        await run_in_threadpool(history.finish_resolution, session, row.id, result, (time.monotonic() - started) * 1000)
        return dict(**result, query_id=row.id)
    await run_in_threadpool(history.fail, session, row, 'resolve', code, message, (time.monotonic() - started) * 1000)
    raise HTTPException(code, message) from None


@router.post('/preview')
def preview(info: PreviewRequest, session: SessionDep, current_user: CurrentUser):
    try:
        return service.preview(session, current_user, info.datasource_id, info.intent, info.resolution_id)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@router.post('/queries/{query_id}/execute')
def execute(query_id: str, info: ConfirmRequest, session: SessionDep, current_user: CurrentUser):
    return service.execute(session, current_user, query_id)


@router.get('/history')
def query_history(datasource_id: int, session: SessionDep, current_user: CurrentUser,
                  page: int = Query(default=1, ge=1, le=100000), page_size: int = Query(default=20, ge=1, le=100)):
    return history.listing(session, current_user, datasource_id, page, page_size)


@router.get('/history/{query_id}')
def query_detail(query_id: str, session: SessionDep, current_user: CurrentUser):
    return history.detail(session, current_user, query_id)
