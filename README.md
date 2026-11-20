# 电商指标工作台

面向电商运营的指标查询工作台，后端使用 FastAPI 和 SQLAlchemy。管理员导入订单并分配数据权限，分析人员可以预览查询范围、执行指标计算，并查看当时使用的数据快照和统计口径。

## 功能

- 初始化令牌保护、PBKDF2 密码哈希、8 小时会话、退出失效、停用账号及单进程登录限速。
- CSV 每次最多导入 10000 行，按数据源保存唯一业务订单号。金额以整数分存储，导入校验和数据库约束共同检查金额范围。
- 数据源授权、服务器地区过滤、管理员用户管理；查询历史按用户隔离。
- 数据库内聚合；筛选值使用绑定参数。五分钟一次性预览，执行前复核权限和数据源版本。
- 查询历史支持游标翻页，并记录数据版本、口径版本、命中行数与耗时。管理员可查看操作审计。

## 本地运行

需要 Python 3.11+，在项目根目录执行：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
export SETUP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s\n' "$SETUP_TOKEN"
uvicorn app.main:app --host 127.0.0.1 --port 18080 --no-proxy-headers
```

打开 http://127.0.0.1:18080 ，展开首次初始化，输入终端中的令牌、用户名和至少 12 位密码。初始化后登录并创建演示数据。默认数据库为 `analytics-v1.db`，重启保留数据。页面令牌只保存在内存中，刷新需要重新登录。不要提交数据库或令牌。

## 演示步骤

1. 初始化、登录、创建演示数据。
2. 查询 2026 年 1 月：支付金额 300.00、订单数 2、客单价 150.00。
3. 查询退款金额：1 月 0.00，2 月 20.00。
4. 创建地区为“华东”的普通用户，授权演示数据源：该用户 1 月支付金额为 100.00。
5. 普通用户生成预览后，管理员改变地区或撤销授权，旧预览无法执行。

可使用 `examples/orders.csv` 导入另一个数据源。每次导入创建不可修改的快照，名称必须唯一，一行一笔订单，业务订单编号在同一快照内唯一，保留前导零。

### 一笔订单跨月退款

在同一次导入中选择 [订单样例](examples/orders.csv) 和 [退款明细样例](examples/refunds.csv)。订单 `demo-001` 于 1 月 31 日退 10.01 元、2 月 1 日退 19.99 元。按“华东”查询这两天，分别得到 10.01 和 19.99 元；查询两天合计 30.00 元，命中 1 笔订单。

退款明细包含 `order_id,refund_id,refund_day,refund` 四列。每笔退款金额为正，退款编号在同一订单内唯一，日期不能早于支付日，累计不能超过支付金额。采用明细的订单在订单 CSV 中填 `refund=0` 并留空 `refund_day`。两份文件每份最多 10000 行，任一明细校验失败会拒绝整次导入。API 调用方在 `POST /api/sources/import` 的请求体中可选传入 `refunds_csv_text`。

## 指标口径 v1

| 指标 | 日期依据 | 计算 |
| --- | --- | --- |
| 支付金额 | day（支付日） | paid 订单的 amount 之和 |
| 支付订单数 | day | paid 订单行数 |
| 客单价 | day | 支付金额 / 订单数；无订单返回 0 |
| 退款金额 | refund_day | 按发生日期汇总已支付订单的退款明细金额 |

范围含首尾业务日期，金额两位小数。执行结果用十进制字符串返回。退款命中数按订单去重，支付指标仍按订单统计。指标口径为固定 v1，不支持动态指标编辑。

## 测试

```bash
python -m pytest -q
node --check app/static/app.js
```

默认使用临时 SQLite 库。有 PostgreSQL 工具时，下面的命令会创建临时集群，跑 HTTP 冒烟和完整测试套件，结束后清理集群。也可以设置 `TEST_POSTGRES_URL` 指向允许创建测试 schema 的专用库，再运行 `python -m pytest -q`。

```bash
PG_BIN="$(pg_config --bindir)" python scripts/postgres_smoke.py --suite
```

`python scripts/benchmark.py` 可复测单个 10000 行 SQLite 快照的 CSV 校验与入库耗时，以及带地区和渠道筛选的数据库聚合耗时。脚本使用临时库，输出数据规模、结果和 预热后 50 次采样的 P50、P95。

浏览器验收脚本 `python scripts/browser_smoke.py` 需另装 Playwright 与 Chromium。脚本启动临时服务，验证导入、查询、历史翻页、审计和移动端布局。

`requirements.lock` 记录本次独立虚拟环境全部验收依赖版本，可使用 `pip install -r requirements.lock` 重现。CI 涵盖 SQLite、PostgreSQL、前端语法和容器启动检查。设计与验收范围见 [DESIGN.md](DESIGN.md) 和 [VERIFICATION.md](VERIFICATION.md)。

## 容器运行

复制 `.env.example` 为 `.env`，填写随机 SETUP_TOKEN，再执行 `docker compose up --build -d`。访问 http://127.0.0.1:18080 。命名卷持久化数据库；`docker compose down -v` 会删除数据。配置只监听回环地址。公网部署需 TLS 反向代理、共享限流、备份与监控；当前实现不是经过生产加固的服务。

首次启动创建 v3 表结构，已有的工作台 v1 或 v2 库会在启动时升级。更新已有部署前先备份并在副本验证。旧库从未保存原始业务订单号，升级会为旧订单生成带标记的内部编号。显式迁移步骤见 [MIGRATION.md](MIGRATION.md)。通过 `DATABASE_URL` 可指定 PostgreSQL 数据库，连接协议为 `postgresql+psycopg://`。

## 接口与维护

- `GET /api/history?limit=20` 返回当前用户的查询记录和 `next_cursor`。传入 `before_id` 继续读取更早一页。无分页参数时仍返回最近 100 条以内的数组，兼容旧客户端。
- `GET /api/audit?limit=20` 需要管理员权限，支持同样的翻页方式。事件包含操作人、操作类型、结果、HTTP 状态和耗时。
- 在与服务相同的数据库配置下执行 `python -m app.cleanup`，清理已过期的预览和会话。成功查询的历史和审计继续保留。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| `app/main.py` | 认证、权限检查和 API 事务 |
| `app/schemas.py`、`app/orders.py` | 请求参数与 CSV 校验 |
| `app/metrics.py` | 指标筛选、聚合和金额计算 |
| `app/models.py`、`app/migrate.py` | 表结构、约束与升级 |
| `app/audit.py`、`app/cleanup.py` | 操作审计、历史分页与过期数据维护 |
| `app/static/`、`examples/` | 浏览器工作台与演示订单 |
