# 电商指标工作台

独立实现的后端作品集项目：FastAPI、SQLAlchemy、关系数据库和无构建依赖的浏览器界面。流程为导入订单、分配访问范围、预览口径、确认执行、查看个人历史。

## 功能

- 初始化令牌保护、PBKDF2 密码哈希、8 小时会话、退出失效、停用账号及单进程登录限速。
- CSV 导入：最多 10000 行、订单编号去重、金额与日期校验、事务整体提交。
- 数据源授权、服务器地区过滤、管理员用户管理；查询历史按用户隔离。
- 数据库内聚合；筛选值使用绑定参数。五分钟一次性预览，执行前复核权限和数据源版本。
- 浏览器工作台、健康检查、Docker Compose 持久化运行配置。

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

可使用 `examples/orders.csv` 导入另一个数据源。每次导入创建不可修改的快照，名称必须唯一，一行一笔订单，订单编号在文件内唯一。

## 指标口径 v1

| 指标 | 日期依据 | 计算 |
| --- | --- | --- |
| 支付金额 | day（支付日） | paid 订单的 amount 之和 |
| 支付订单数 | day | paid 订单行数 |
| 客单价 | day | 支付金额 / 订单数；无订单返回 0 |
| 退款金额 | refund_day | paid 订单的 refund 之和 |

范围含首尾业务日期，金额两位小数。执行结果用十进制字符串返回。v1 每笔订单仅表达一个汇总退款及一个退款日期，不支持跨日多次退款。指标口径为固定 v1，不支持动态指标编辑。

## 测试

```bash
pytest -q
node --check app/static/app.js
```

测试使用临时库，覆盖授权、地区过滤、退款日期、注入输入、重复及过期预览、权限撤销、退出和导入原子性。浏览器验收脚本 `scripts/browser_smoke.py` 需另装 Playwright 与 Chromium，会自动创建临时数据库和服务。

`requirements.lock` 记录本次独立虚拟环境全部验收依赖版本，可使用 `pip install -r requirements.lock` 重现。PostgreSQL 验收：设置 PG_BIN 为 PostgreSQL 可执行文件目录后运行 `python scripts/postgres_smoke.py`；脚本创建并自动清理独立临时集群。设计说明见 DESIGN.md，实际验收范围见 VERIFICATION.md。

## 容器运行

复制 `.env.example` 为 `.env`，填写随机 SETUP_TOKEN，再执行 `docker compose up --build -d`。访问 http://127.0.0.1:18080 。命名卷持久化数据库；`docker compose down -v` 会删除数据。配置只监听回环地址。公网部署需 TLS 反向代理、共享限流、备份与监控；当前实现不是经过生产加固的服务。

首次启动创建 v1 数据库表。检测到不兼容的早期原型结构时会拒绝启动，应使用新的 v1 数据库；不自动迁移早期原型数据。可通过 DATABASE_URL 指定 SQLAlchemy 地址，PostgreSQL 使用 postgresql+psycopg:// 协议。

## 范围与代码

数据接入使用 CSV 快照，不提供外部业务库实时连接、自动同步、模型问答或任意 SQL 编辑。`app/main.py` 实现数据模型与后端接口，`app/static/` 是 HTML/CSS/JavaScript 工作台，`tests/` 为接口和数据库回归测试，`examples/` 为合成数据。

此实现不依赖 SQLBot 及其扩展包。第三方框架、数据库、库的许可仍属于对应作者。本项目是范围明确的指标工作流，不宣称覆盖大型 BI 平台全部功能。
