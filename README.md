# 电商指标问数

面向电商分析场景的指标查询项目。

支持支付金额、支付订单数、客单价和退款金额，包含版本化业务口径、结构化问题解析、参数化 SQL、预览确认、权限重检、只读执行、查询历史和自动评测。

## 代码结构

- `sqlbot/backend/apps/metrics/`：指标定义、问题解析、SQL 编译、查询执行及历史接口。
- `sqlbot/backend/alembic/versions/073_metric_tools.py`、`074_metric_history.py`：数据库迁移。
- `sqlbot/frontend/src/views/metrics/`：指标工作台与历史页面。
- `sqlbot/backend/tests/test_metric*.py`：指标与权限测试。
- `sqlbot/backend/evals/metrics/`：合成评测样例。
- `sqlbot/backend/scripts/`：模拟数据初始化及评测命令。
- `local_runtime.py`：本地服务管理与前端构建入口。

## 开发环境

Python 3.11、PostgreSQL/pgvector、Node.js。依赖声明位于 `sqlbot/backend/pyproject.toml`、`sqlbot/frontend/package.json` 和 `sqlbot/g2-ssr/package.json`。本地 Python 安装使用 `constraints-local.txt` 中的版本约束。

部署前需安装依赖、初始化 PostgreSQL、设置环境变量、执行 Alembic 迁移并构建前端。`.env.example` 仅提供配置项，不含凭据。启动前必须通过环境变量设置非空的 `POSTGRES_PASSWORD` 和 `DEFAULT_PWD`；安装脚本对应使用 `SQLBOT_DB_PASSWORD` 和 `SQLBOT_DEFAULT_PWD`，不再提供通用默认密码。数据源配置还需要 16、24 或 32 字节的 `DS_AES_KEY`，构建前端时通过 `--build-arg VITE_DS_AES_KEY=<相同值>` 传入。模型在应用管理界面配置；未配置模型时可手动选择参数查询。

`local_runtime.py` 适用于已配置的本地环境：Python 和 PostgreSQL 可执行文件位于 `runtime/env/bin/`，数据库目录为 `data/postgresql-local/`，向量模型位于 `runtime/models/`。它不负责下载依赖或初始化数据库。

```bash
# 已配置环境中的前端构建与服务管理
runtime/env/bin/python local_runtime.py build_frontend
bash start.sh
bash status.sh
bash stop.sh

# 后端测试：在已安装依赖的 Python 环境中运行
cd sqlbot/backend
python -m pytest -q
```

PostgreSQL 隔离测试需显式设置 `METRICS_PG_TEST=1` 并提供数据库连接配置。评测命令参数见 `python scripts/evaluate_metrics.py --help`。

## 许可证

沿用 SQLBot 的许可证，见 [LICENSE](LICENSE)。保留上游版权、商标及前端标识；第三方组件许可证随源码保留。
