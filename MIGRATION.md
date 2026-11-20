# 数据库结构升级到 v5

从项目根目录运行。应用启动和 `python -m app.migrate` 使用同一个迁移入口，目标库由 `DATABASE_URL` 决定。空库直接创建 v5。已有 v1 至 v4 库在事务内升级到 v5，重复执行只校验现有结构。默认文件名仍为 `analytics-v1.db`。指标口径版本仍为 1。

## 在副本上演练

先停止应用、后台作业及其他写入进程。选择一个此前不存在的备份和演练库名。保留旧版程序及依赖，用备份恢复的独立副本先跑一次迁移。升级会重写订单与历史，运行时间、临时空间、日志空间随数据量增加。

**SQLite**。以下示例使用默认数据库，SQLite 命令行工具的在线备份接口会包含已经提交的 WAL 数据。命令在项目根目录执行。

```bash
sqlite3 -readonly analytics-v1.db ".backup 'analytics-pre-v2.backup.db'"
cp -n analytics-pre-v2.backup.db analytics-rehearsal.db
DATABASE_URL=sqlite:///./analytics-rehearsal.db python -m app.migrate
sqlite3 -readonly analytics-rehearsal.db 'SELECT version FROM schema_version WHERE id=1'
sqlite3 -readonly analytics-rehearsal.db 'PRAGMA integrity_check'
sqlite3 -readonly analytics-rehearsal.db 'PRAGMA foreign_key_check'
```

预期版本为 5，完整性检查返回 `ok`，外键检查返回零行。正式升级时，在同一目录使用实际文件地址运行 `DATABASE_URL=sqlite:///./analytics-v1.db python -m app.migrate`。Compose 部署的数据文件在卷内的 `/data/analytics-v1.db`，应先停止服务并备份实际卷内数据库，再用新版镜像执行 `docker compose run --rm --no-deps analytics python -m app.migrate`。

**PostgreSQL**。以下命令使用本机连接参数和数据库名 `analytics`，也可通过标准 libpq 环境变量指定主机、端口和用户。迁移账号需要对应用表和序列执行 DDL 的权限。应用连接地址使用 SQLAlchemy 的 `postgresql+psycopg` 协议。

```bash
pg_dump -Fc -d analytics -f analytics-pre-v2.dump
createdb analytics_rehearsal
pg_restore --exit-on-error -d analytics_rehearsal analytics-pre-v2.dump
DATABASE_URL=postgresql+psycopg:///analytics_rehearsal python -m app.migrate
psql -d analytics_rehearsal -c 'SELECT version FROM schema_version WHERE id=1'
```

演练通过后，使用实际连接地址执行 `DATABASE_URL=postgresql+psycopg:///analytics python -m app.migrate`。升级持有事务级建议锁，并对被修改表加锁。SQLite 使用 `BEGIN IMMEDIATE` 在检查和重建前取得写锁。版本记录和数据转换在同一事务中提交。

## v4 升级到 v5

已有报告增加两个可空账期列，并建立按快照和创建时间翻页的索引。旧记录的账期为空，继续表示整份快照；报告编号、创建时间、摘要和原始内容保持原值。PostgreSQL 同时在订单表创建用于退款汇总的组合索引；在大库升级前请在副本上测量建索引时间并预留空间。升级后可从历史列表打开旧报告，或在整份快照模式再次上传同一账单验证编号一致。

## v3 升级到 v4

此次升级在事务内创建退款对账报告表和快照内唯一账单摘要约束。原订单、退款事件和历史记录继续使用。首次对账后可用同一快照重复上传账单，核对两次的报告编号一致。

## 退款事件回填

v2 升级会创建 `refund_events`，每条已有退款日期的订单生成一条事件，保留原金额、日期和订单主键，业务退款编号留空。v1 先转换金额再执行同样的回填。版本标记与回填一起提交。首次升级完成后，在导入新数据前，可对比 `SELECT count(*), sum(refund_cents) FROM orders WHERE refund_day IS NOT NULL` 与 `SELECT count(*), sum(amount_cents) FROM refund_events`。历史查询保留原结果。

## 对账与历史语义

- 升级前后比对 `orders`、`query_history`、`users`、`sessions`、`grants`、`previews` 的行数。抽查相同订单主键、业务日期、用户授权和历史主键。确认原 v1 订单均标记为合成编号，再以原账户执行一次查询，核对结果和新增历史。
- `orders.order_id` 保存新 CSV 的原始订单编号，包括前导零。每个数据源内唯一，数据库约束与 CSV 校验共同限制金额、退款和编号。v1 曾丢弃原始编号，迁移保留行主键，使用 `legacy-<原主键>` 并将 `order_id_synthetic` 设为真。v1 个别订单若没有关联数据源，迁入一个独立、初始无用户授权的“v1 未归属订单”快照，名称冲突时加数字后缀。
- `orders.amount_cents`、`orders.refund_cents` 与 `query_history.value_cents` 使用 64 位整数存储。单笔上限为 999,999,999,999.99 元，单源上限 10,000 行，对应最大合计 999,999,999,999,990,000 分。支付订单数的历史值也按两位小数编码，例如 `value_cents=200` 表示 2 单。客单价以十进制执行除法，按分舍入，中点取偶。
- SQLite 的 v1 `NUMERIC` 列可能已保存为浮点数。迁移按 v1 对历史值的两位小数显示结果转换。已经在 v1 丢失的原始精度只能靠业务凭证或 CSV 复核。v2 新导入和聚合均按整数分处理。
- v1 没有记录历史的预览 ID、执行时数据源名称和版本、口径版本、匹配行数或耗时，这些旧记录字段保留为空。审计表从升级后开始记录。已有预览保留原有效期。
- SQLite 重建两张表时每批转换 1,000 行。程序会检查相关自定义索引、触发器、视图和指向这两张表的外键，遇到依赖会停止。此升级适用于项目发布的 v1 结构，手工修改过列定义或表约束的库应在副本上单独适配。PostgreSQL 原地修改列，并保留表和序列，显式插入的历史主键若领先于序列，会同步序列位置。

## 失败恢复

任一转换或数据库约束失败时，升级事务回滚。保留备份和错误信息，在副本上检查异常行、金额范围和数据库结构，不要手工写入版本号。若已成功升级但需要使用旧程序，停止写入，将 SQLite 备份复制到新的数据库文件，或将 PostgreSQL 归档恢复至新的空库，然后让旧程序连接该恢复库。恢复点为备份时刻，升级后的新增业务记录须另行对账。

## 自动化验证

```bash
python -m pytest -q tests/test_storage.py
PG_BIN=/path/to/postgresql/bin python scripts/postgres_smoke.py --suite
```

第二条命令创建一次性 PostgreSQL 集群，并同时运行 SQLite 和 PostgreSQL 用例。迁移测试涵盖数据与主键保留、重复运行、失败回滚、旧 SQLite 大额历史和自定义依赖。金额测试导入 10,000 笔接近单笔上限的订单，通过预览执行和历史读取核对到分。
