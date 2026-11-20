import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query as QueryParam, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from . import audit, reconciliation
from .metrics import aggregate
from .models import AuditEvent, Base, Bootstrap, Grant, History, Order, Preview, Reconciliation, Source, Token, User
from .orders import parse_orders
from .schemas import Access, ChannelBill, Credentials, ImportCSV, NewUser, Query


DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./analytics-v1.db')
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite') else {})
SessionLocal = sessionmaker(bind=engine)

def init_db():
    from app.migrate import upgrade
    upgrade(engine)

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title='电商指标服务', lifespan=lifespan)
attempts = defaultdict(deque)
attempt_lock = threading.Lock()

@app.middleware('http')
async def request_limits(request: Request, call_next):
    if request.url.path in ('/api/login', '/api/setup'):
        key = request.client.host if request.client else 'local'
        now = time.monotonic()
        with attempt_lock:
            for stale in list(attempts):
                if not attempts[stale] or attempts[stale][-1] <= now - 60:
                    del attempts[stale]
            queue = attempts[key]
            while queue and queue[0] <= now - 60:
                queue.popleft()
            if len(queue) >= 20:
                return JSONResponse({'detail': '请求过于频繁，请一分钟后再试'}, status_code=429)
            queue.append(now)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if request.url.path == '/':
        response.headers['Content-Security-Policy'] = "default-src 'self'; object-src 'none'; frame-ancestors 'none'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600_000).hex()
    return salt + '$' + hashed

def session_actor(db, authorization):
    if not authorization or not authorization.startswith('Bearer '):
        return None
    digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
    return db.scalar(select(User).join(Token, Token.user_id == User.id).where(
        Token.digest == digest, Token.expires > datetime.utcnow()
    ))


def require_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, '请登录')
    with SessionLocal() as db:
        user = session_actor(db, authorization)
        if user is not None:
            audit.identify_actor(user.id)
        if not user or not user.active:
            raise HTTPException(401, '登录已失效')
        return {'id': user.id, 'region': user.region, 'admin': user.admin}


app.router.route_class = audit.audited_routes(lambda: SessionLocal(), session_actor)


@app.post('/api/setup', status_code=201)
def setup(data: Credentials, x_setup_token: str | None = Header(default=None)):
    expected = os.environ.get('SETUP_TOKEN', '')
    if not expected or not hmac.compare_digest(expected, x_setup_token or ''):
        raise HTTPException(403, '需要服务端配置的初始化令牌')
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(User)):
            raise HTTPException(403, '初始化已完成')
        db.add(Bootstrap(id=1))
        admin = User(username=data.username, password_hash=hash_password(data.password), admin=True)
        db.add(admin)
        try:
            db.flush()
            audit.success(db, 'system.setup', admin.id, 201)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, '初始化已由其他请求完成')
    return {'message': '初始化完成'}

@app.post('/api/login')
def login(data: Credentials):
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == data.username))
        if not user or not user.active or not hmac.compare_digest(user.password_hash, hash_password(data.password, user.password_hash.split('$')[0])):
            raise HTTPException(401, '用户名或密码错误')
        token = secrets.token_urlsafe(32)
        db.add(Token(user_id=user.id, digest=hashlib.sha256(token.encode()).hexdigest(), expires=datetime.utcnow() + timedelta(hours=8)))
        db.commit()
    return {'access_token': token}

@app.post('/api/demo')
def demo(user=Depends(require_user)):
    if not user['admin']:
        raise HTTPException(403, '需要管理员权限')
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(Order)):
            raise HTTPException(409, '已有数据')
        source = Source(name='演示订单')
        db.add(source)
        db.flush()
        rows = parse_orders(
            'order_id,day,region,channel,amount,status,refund,refund_day\n'
            'demo-1,2026-01-10,华东,网页,100.00,paid,0,\n'
            'demo-2,2026-01-11,华北,移动端,200.00,paid,20.00,2026-02-01\n'
        )
        for row in rows:
            row.source_id = source.id
        db.add_all(rows)
        audit.success(db, 'source.demo', user['id'], source_id=source.id, rows=2)
        db.commit()
    return {'message': '示例数据已创建'}

PageLimit = Annotated[int, QueryParam(ge=1, le=100)]
OptionalPageLimit = Annotated[int | None, QueryParam(ge=1, le=100)]
PageCursor = Annotated[int | None, QueryParam(ge=1, le=2**63 - 1)]


@app.get('/api/history')
def history(user=Depends(require_user), limit: OptionalPageLimit = None, before_id: PageCursor = None):
    with SessionLocal() as db:
        own_history = select(History).where(History.user_id == user['id'])
        result = audit.page(db, own_history, History.id, audit.history_item, limit or 100, before_id)
        if limit is None and before_id is None:
            return result['items']
        return result


@app.get('/api/audit')
def audit_log(user=Depends(require_user), limit: PageLimit = 20, before_id: PageCursor = None):
    admin_only(user)
    with SessionLocal() as db:
        return audit.page(db, select(AuditEvent), AuditEvent.id, audit.audit_item, limit, before_id)


@app.get('/', response_class=HTMLResponse)
def home():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')

def admin_only(user):
    if not user['admin']:
        raise HTTPException(403, '需要管理员权限')

def source_access(db, source_id, user):
    source = db.get(Source, source_id)
    if not source or (not user['admin'] and not db.get(Grant, (user['id'], source_id))):
        raise HTTPException(403, '无数据源访问权限')
    return source


@app.get('/api/me')
def me(user=Depends(require_user)):
    return user

@app.get('/api/health')
def health():
    with SessionLocal() as db:
        db.execute(select(1))
    return {'status': 'ok'}

@app.post('/api/logout')
def logout(user=Depends(require_user), authorization: str = Header()):
    with SessionLocal() as db:
        db.execute(delete(Token).where(Token.digest == hashlib.sha256(authorization[7:].encode()).hexdigest()))
        db.commit()
    return {'message': '已退出'}

@app.get('/api/users')
def users(user=Depends(require_user)):
    admin_only(user)
    with SessionLocal() as db:
        return [{'id': u.id, 'username': u.username, 'region': u.region, 'admin': u.admin, 'active': u.active, 'source_ids': list(db.scalars(select(Grant.source_id).where(Grant.user_id == u.id)))} for u in db.scalars(select(User))]

def set_grants(db, uid, ids):
    if any(db.get(Source, sid) is None for sid in ids):
        raise HTTPException(422, '数据源不存在')
    db.execute(delete(Grant).where(Grant.user_id == uid))
    db.add_all([Grant(user_id=uid, source_id=sid) for sid in set(ids)])

def lock_access_user(db, uid):
    # Both operations take this lock before reading grants, even for a grant-only change.
    # SQLite has no row locks, so reserve its writer before taking a read snapshot.
    if db.get_bind().dialect.name == 'sqlite':
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    return db.scalar(select(User).where(User.id == uid).with_for_update())

@app.post('/api/users', status_code=201)
def create_user(data: NewUser, user=Depends(require_user)):
    admin_only(user)
    with SessionLocal() as db:
        u = User(username=data.username, password_hash=hash_password(data.password), region=data.region, active=data.active)
        db.add(u)
        try:
            db.flush()
            set_grants(db, u.id, data.source_ids)
            audit.success(db, 'user.create', user['id'], 201, target_user_id=u.id,
                          source_ids=sorted(set(data.source_ids)), region=data.region, active=data.active)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, '用户名已存在')
        return {'id': u.id}

@app.put('/api/users/{uid}/access')
def update_access(uid: int, data: Access, user=Depends(require_user)):
    admin_only(user)
    audit.add_context(target_user_id=uid)
    with SessionLocal() as db:
        u = lock_access_user(db, uid)
        if not u:
            raise HTTPException(404, '用户不存在')
        if u.admin:
            raise HTTPException(409, '不能通过此接口修改管理员')
        u.region, u.active = data.region, data.active
        set_grants(db, uid, data.source_ids)
        audit.success(db, 'user.access', user['id'], target_user_id=uid,
                      source_ids=sorted(set(data.source_ids)), region=data.region, active=data.active)
        db.commit()
    return {'message': '权限已更新'}

@app.get('/api/sources')
def sources(user=Depends(require_user)):
    with SessionLocal() as db:
        stmt = select(Source)
        if not user['admin']:
            stmt = stmt.join(Grant).where(Grant.user_id == user['id'])
        return [{'id': s.id, 'name': s.name, 'version': s.version} for s in db.scalars(stmt)]


@app.post('/api/sources/import', status_code=201)
def import_csv(data: ImportCSV, user=Depends(require_user)):
    admin_only(user)
    rows = parse_orders(data.csv_text, data.refunds_csv_text)
    with SessionLocal() as db:
        source = Source(name=data.name)
        db.add(source)
        try:
            db.flush()
            for row in rows:
                row.source_id = source.id
            db.add_all(rows)
            audit.success(db, 'source.import', user['id'], 201, source_id=source.id,
                          source_version=source.version, rows=len(rows))
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, '数据源名称已存在')
        return {'id': source.id, 'rows': len(rows)}

@app.post('/api/sources/{source_id}/reconciliations')
def reconcile_refunds(source_id: int, data: ChannelBill, user=Depends(require_user)):
    admin_only(user)
    audit.add_context(source_id=source_id)
    channel = reconciliation.parse_bill(data.csv_text)
    with SessionLocal.begin() as db:
        if db.get(Source, source_id) is None:
            raise HTTPException(404, '数据源不存在')
        record, reused = reconciliation.save(db, source_id, user['id'], channel)
        audit.success(db, 'refund.reconcile', user['id'], source_id=source_id,
                      reconciliation_id=record.id, reused=reused)
        return reconciliation.present(record)


@app.get('/api/reconciliations/{report_id}')
def get_reconciliation(report_id: int, user=Depends(require_user)):
    admin_only(user)
    with SessionLocal() as db:
        record = db.get(Reconciliation, report_id)
        if record is None:
            raise HTTPException(404, '对账报告不存在')
        return reconciliation.present(record)


@app.post('/api/previews')
def preview(data: Query, user=Depends(require_user)):
    audit.add_context(source_id=data.source_id, metric=data.metric)
    if data.start_date > data.end_date or (data.end_date-data.start_date).days > 3660:
        raise HTTPException(422, '日期范围无效或超过十年')
    with SessionLocal() as db:
        source = source_access(db, data.source_id, user)
        p = Preview(id=secrets.token_urlsafe(24), user_id=user['id'], payload=data.model_dump_json(), region=user['region'], source_version=source.version, expires=datetime.utcnow()+timedelta(minutes=5))
        db.add(p)
        audit.success(db, 'query.preview', user['id'], preview_id=p.id, source_id=source.id,
                      source_version=source.version, metric=data.metric)
        db.commit()
        return {'id': p.id, 'query': data.model_dump(), 'region': p.region, 'source': source.name, 'definition_version': 1, 'expires': p.expires, 'date_basis': audit.date_basis(data.metric)}

@app.post('/api/previews/{pid}/execute')
def execute_preview(pid: str, user=Depends(require_user)):
    started = time.monotonic()
    with SessionLocal() as db:
        current = lock_access_user(db, user['id'])
        if current is None:
            raise HTTPException(401, '登录已失效')
        p = db.get(Preview, pid)
        if not p or p.user_id != user['id']:
            raise HTTPException(404, '预览不存在')
        audit.add_context(preview_id=p.id, source_version=p.source_version)
        if p.used or p.expires <= datetime.utcnow():
            raise HTTPException(409, '预览已执行或过期，请重新预览')
        data = Query.model_validate_json(p.payload)
        audit.add_context(source_id=data.source_id, metric=data.metric)
        fresh = {'id': current.id, 'region': current.region, 'admin': current.admin}
        source = source_access(db, data.source_id, fresh)
        if not current.active or p.region != current.region or p.source_version != source.version:
            raise HTTPException(409, '权限或数据版本已变化，请重新预览')
        claimed = db.execute(update(Preview).where(Preview.id == pid, Preview.used == False).values(used=True))
        if claimed.rowcount != 1:
            raise HTTPException(409, '预览已执行')
        value, count = aggregate(db, data, fresh)
        duration = audit.elapsed_ms(started)
        history_row = History(
            user_id=user['id'], preview_id=pid, source_id=source.id, source_name=source.name,
            source_version=source.version, definition_version=1, channel=data.channel,
            region=fresh['region'], metric=data.metric, start_date=data.start_date, end_date=data.end_date,
            value=value, matched_count=count, duration_ms=duration,
        )
        db.add(history_row)
        db.flush()
        audit.success(db, 'query.execute', user['id'], history_id=history_row.id, preview_id=pid,
                      source_id=source.id, source_version=source.version, metric=data.metric,
                      definition_version=1, matched_count=count)
        db.commit()
        return {'value': str(value), 'count': count, 'metric': data.metric, 'definition_version': 1}

app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static', check_dir=False), name='static')
