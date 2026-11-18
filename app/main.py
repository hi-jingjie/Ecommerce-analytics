import hashlib
import hmac
import os
import secrets
import csv
import io
import json
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, create_engine, func, select, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy import inspect
import time
import threading
from collections import defaultdict, deque
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./analytics-v1.db')
engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False} if DATABASE_URL.startswith('sqlite') else {})
SessionLocal = sessionmaker(bind=engine)

class Base(DeclarativeBase):
    pass

class Bootstrap(Base):
    __tablename__ = 'bootstrap'
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)
    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

class Source(Base):
    __tablename__ = 'sources'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    version: Mapped[int] = mapped_column(default=1)

class Grant(Base):
    __tablename__ = 'grants'
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey('sources.id'), primary_key=True)

class Preview(Base):
    __tablename__ = 'previews'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    payload: Mapped[str] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_version: Mapped[int] = mapped_column(Integer)
    expires: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(Boolean, default=False)

class Token(Base):
    __tablename__ = 'sessions'
    id: Mapped[int] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    expires: Mapped[datetime] = mapped_column(DateTime)

class Order(Base):
    __tablename__ = 'orders'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey('sources.id'), nullable=True, index=True)
    day: Mapped[date] = mapped_column(Date)
    region: Mapped[str] = mapped_column(String(80))
    channel: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    refund: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    refund_day: Mapped[date | None] = mapped_column(Date, nullable=True)

class History(Base):
    __tablename__ = 'query_history'
    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel: Mapped[str | None] = mapped_column(String(80), nullable=True)
    region: Mapped[str | None] = mapped_column(String(80), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    metric: Mapped[str] = mapped_column(String(40))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

def init_db():
    # Refuse to silently run against an incompatible prototype schema.
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if inspector.has_table(table.name):
            actual = {column['name'] for column in inspector.get_columns(table.name)}
            missing = set(table.columns.keys()) - actual
            if missing:
                raise RuntimeError(f'Incompatible database schema for {table.name}: {sorted(missing)}. Use a fresh v1 database or migrate a backup first.')
    Base.metadata.create_all(engine)

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

class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=12, max_length=128)

class Query(BaseModel):
    source_id: int = Field(default=1, ge=1)
    metric: Literal['payment_amount', 'paid_orders', 'average_order_value', 'refund_amount']
    start_date: date
    end_date: date
    channel: str | None = Field(default=None, max_length=80)

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 600_000).hex()
    return salt + '$' + hashed

def require_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, '请登录')
    digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
    with SessionLocal() as db:
        session = db.scalar(select(Token).where(Token.digest == digest, Token.expires > datetime.utcnow()))
        user = db.get(User, session.user_id) if session else None
        if not user or not user.active:
            raise HTTPException(401, '登录已失效')
        return {'id': user.id, 'region': user.region, 'admin': user.admin}

@app.post('/api/setup', status_code=201)
def setup(data: Credentials, x_setup_token: str | None = Header(default=None)):
    expected = os.environ.get('SETUP_TOKEN', '')
    if not expected or not hmac.compare_digest(expected, x_setup_token or ''):
        raise HTTPException(403, '需要服务端配置的初始化令牌')
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(User)):
            raise HTTPException(403, '初始化已完成')
        db.add(Bootstrap(id=1))
        db.add(User(username=data.username, password_hash=hash_password(data.password), admin=True))
        try:
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
        db.add_all([Order(source_id=source.id, day=date(2026, 1, 10), region='华东', channel='网页', status='paid', amount=Decimal('100'), refund=Decimal('0')), Order(source_id=source.id, day=date(2026, 1, 11), region='华北', channel='移动端', status='paid', amount=Decimal('200'), refund=Decimal('20'), refund_day=date(2026, 2, 1))])
        db.commit()
    return {'message': '示例数据已创建'}

def query(data: Query, user=Depends(require_user)):
    if data.start_date > data.end_date:
        raise HTTPException(422, '开始日期不能晚于结束日期')
    with SessionLocal() as db:
        value, count = aggregate(db, data, user)
        db.add(History(user_id=user['id'], source_id=data.source_id, metric=data.metric, start_date=data.start_date, end_date=data.end_date, value=value))
        db.commit()
        return {'value': float(value), 'count': count}

@app.get('/api/history')
def history(user=Depends(require_user)):
    with SessionLocal() as db:
        rows = db.scalars(select(History).where(History.user_id == user['id']).order_by(History.id.desc()).limit(100)).all()
        return [{'id': r.id, 'metric': r.metric, 'value': str(r.value), 'start_date': r.start_date, 'end_date': r.end_date} for r in rows]

@app.get('/', response_class=HTMLResponse)
def home():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')

def aggregate(db, data, user):
    day = Order.refund_day if data.metric == 'refund_amount' else Order.day
    stmt = select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0), func.coalesce(func.sum(Order.refund), 0)).where(day >= data.start_date, day <= data.end_date, Order.status == 'paid', Order.source_id == data.source_id)
    if user['region'] is not None:
        stmt = stmt.where(Order.region == user['region'])
    if data.channel:
        stmt = stmt.where(Order.channel == data.channel)
    count, total, refunds = db.execute(stmt).one()
    value = {'payment_amount': total, 'paid_orders': count, 'average_order_value': total / count if count else 0, 'refund_amount': refunds}[data.metric]
    return Decimal(value).quantize(Decimal('.01')), count

def admin_only(user):
    if not user['admin']:
        raise HTTPException(403, '需要管理员权限')

def source_access(db, source_id, user):
    source = db.get(Source, source_id)
    if not source or (not user['admin'] and not db.get(Grant, (user['id'], source_id))):
        raise HTTPException(403, '无数据源访问权限')
    return source

class Access(BaseModel):
    region: str | None = Field(default=None, min_length=1, max_length=80)
    source_ids: list[int] = Field(default_factory=list, max_length=100)
    active: bool = True

class NewUser(Credentials, Access):
    pass

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

@app.post('/api/users', status_code=201)
def create_user(data: NewUser, user=Depends(require_user)):
    admin_only(user)
    with SessionLocal() as db:
        u = User(username=data.username, password_hash=hash_password(data.password), region=data.region, active=data.active)
        db.add(u)
        try:
            db.flush()
            set_grants(db, u.id, data.source_ids)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, '用户名已存在')
        return {'id': u.id}

@app.put('/api/users/{uid}/access')
def update_access(uid: int, data: Access, user=Depends(require_user)):
    admin_only(user)
    with SessionLocal() as db:
        u = db.get(User, uid)
        if not u:
            raise HTTPException(404, '用户不存在')
        if u.admin:
            raise HTTPException(409, '不能通过此接口修改管理员')
        u.region, u.active = data.region, data.active
        set_grants(db, uid, data.source_ids)
        db.commit()
    return {'message': '权限已更新'}

@app.get('/api/sources')
def sources(user=Depends(require_user)):
    with SessionLocal() as db:
        stmt = select(Source)
        if not user['admin']:
            stmt = stmt.join(Grant).where(Grant.user_id == user['id'])
        return [{'id': s.id, 'name': s.name, 'version': s.version} for s in db.scalars(stmt)]

class ImportCSV(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    csv_text: str = Field(min_length=1, max_length=2_000_000)

@app.post('/api/sources/import', status_code=201)
def import_csv(data: ImportCSV, user=Depends(require_user)):
    admin_only(user)
    reader = csv.DictReader(io.StringIO(data.csv_text))
    required = {'order_id', 'day', 'region', 'channel', 'amount', 'status', 'refund', 'refund_day'}
    if set(reader.fieldnames or []) != required:
        raise HTTPException(422, 'CSV 列必须为 '+','.join(sorted(required)))
    rows, ids = [], set()
    try:
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError('字段数量与表头不一致')
            if len(rows) >= 10000 or not row['order_id'] or row['order_id'] in ids:
                raise ValueError('订单编号重复、为空或超过 10000 行')
            ids.add(row['order_id'])
            amount, refund = Decimal(row['amount']), Decimal(row['refund'] or '0')
            if not amount.is_finite() or not refund.is_finite() or not (0 <= refund <= amount < 10**12) or amount != amount.quantize(Decimal('.01')) or refund != refund.quantize(Decimal('.01')):
                raise ValueError('金额必须非负且最多两位小数，退款不超过支付金额')
            if row['status'] not in ('paid', 'cancelled') or not all(0 < len(row[k]) <= 80 for k in ('region', 'channel')):
                raise ValueError('状态、地区或渠道无效')
            day = date.fromisoformat(row['day'])
            refund_day = date.fromisoformat(row['refund_day']) if row['refund_day'] else None
            if refund and (not refund_day or refund_day < day or row['status'] != 'paid'):
                raise ValueError('退款必须提供不早于支付日期的退款日期且订单已支付')
            rows.append(Order(day=day, region=row['region'], channel=row['channel'], amount=amount, status=row['status'], refund=refund, refund_day=refund_day))
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise HTTPException(422, f'CSV 第 {len(rows)+2} 行格式错误：{exc}')
    if not rows:
        raise HTTPException(422, 'CSV 没有数据')
    with SessionLocal() as db:
        source = Source(name=data.name)
        db.add(source)
        try:
            db.flush()
            for row in rows:
                row.source_id = source.id
            db.add_all(rows)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, '数据源名称已存在')
        return {'id': source.id, 'rows': len(rows)}

@app.post('/api/previews')
def preview(data: Query, user=Depends(require_user)):
    if data.start_date > data.end_date or (data.end_date-data.start_date).days > 3660:
        raise HTTPException(422, '日期范围无效或超过十年')
    with SessionLocal() as db:
        source = source_access(db, data.source_id, user)
        p = Preview(id=secrets.token_urlsafe(24), user_id=user['id'], payload=data.model_dump_json(), region=user['region'], source_version=source.version, expires=datetime.utcnow()+timedelta(minutes=5))
        db.add(p)
        db.commit()
        return {'id': p.id, 'query': data.model_dump(), 'region': p.region, 'source': source.name, 'definition_version': 1, 'expires': p.expires, 'date_basis': '退款发生日' if data.metric == 'refund_amount' else '支付日'}

@app.post('/api/previews/{pid}/execute')
def execute_preview(pid: str, user=Depends(require_user)):
    with SessionLocal() as db:
        p = db.get(Preview, pid)
        if not p or p.user_id != user['id']:
            raise HTTPException(404, '预览不存在')
        if p.used or p.expires <= datetime.utcnow():
            raise HTTPException(409, '预览已执行或过期，请重新预览')
        data = Query.model_validate_json(p.payload)
        current = db.get(User, user['id'])
        fresh = {'id': current.id, 'region': current.region, 'admin': current.admin}
        source = source_access(db, data.source_id, fresh)
        if not current.active or p.region != current.region or p.source_version != source.version:
            raise HTTPException(409, '权限或数据版本已变化，请重新预览')
        claimed = db.execute(update(Preview).where(Preview.id == pid, Preview.used == False).values(used=True))
        if claimed.rowcount != 1:
            raise HTTPException(409, '预览已执行')
        value, count = aggregate(db, data, fresh)
        db.add(History(user_id=user['id'], source_id=data.source_id, channel=data.channel, region=fresh['region'], metric=data.metric, start_date=data.start_date, end_date=data.end_date, value=value))
        db.commit()
        return {'value': str(value), 'count': count, 'metric': data.metric, 'definition_version': 1}

app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static', check_dir=False), name='static')
