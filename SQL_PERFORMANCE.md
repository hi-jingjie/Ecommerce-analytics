# PostgreSQL 17 退款聚合执行计划

同一快照和地区下，查询一天的退款时，增加订单复合索引使本机执行时间中位数从 1.285 ms 降至 0.292 ms。查询整个季度时，从 6.405 ms 降至 5.417 ms。两次优化均来自订单侧减少了 103 次共享缓冲页访问。宽窗口仍需扫描退款明细表，收益较小。

## 查询与样本

脚本调用 `app.metrics.aggregate`，传入退款金额指标、目标快照 8 和用户地区 `region-14`，捕获实际发送给驱动的 SELECT 和绑定参数，再用同一组参数运行 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`。下面是便于阅读的 SQL，`:start_date` 和 `:end_date` 含首尾两天。

```sql
SELECT count(DISTINCT o.id), coalesce(sum(r.amount_cents), 0)
FROM orders AS o
JOIN refund_events AS r ON r.order_pk = o.id
WHERE r.day BETWEEN :start_date AND :end_date
  AND o.status = 'paid' AND o.source_id = 8 AND o.region = 'region-14';
```

16 个快照各含 8,000 笔订单和 2,909 笔退款明细，共 128,000 笔订单、46,544 笔明细。生成器按快照重复使用业务订单号，退款通过订单主键关联。31 个地区轮流分配，每 11 笔订单有 1 笔取消，部分已支付订单发生两笔跨日退款。金额按快照及该笔退款次序生成，因此可以分别验证金额总和与去重订单数。

基线保留 `orders(source_id)`、`refund_events(day, order_pk)`、两张表的主键和业务唯一约束。在同一个测试库、同一份数据上执行以下 DDL 后重复测量。

```sql
CREATE INDEX ix_orders_source_region_status_id
    ON orders (source_id, region, status, id);
```

## 实测结果

PostgreSQL 17.11，Linux x86_64，Intel Xeon Platinum 8352V。本次查询使用热缓存，各组先执行一次应用查询和一次预热计划，再取七次 `Execution Time` 的中位数。表中页数取该次执行的根节点，包含子节点访问，重复访问同一页也计数。时间仅覆盖服务端执行；HTTP、SQL 规划、导入和建索引耗时均在计时范围之外。

| 退款日期 | 索引 | 执行中位数（ms） | 共享页命中 | 共享页读取 | 去重订单 | 金额（元） |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2 月 15 日 | 基线 | 1.285 | 438 | 0 | 1 | 1.08 |
| 2 月 15 日 | 加复合索引 | 0.292 | 335 | 0 | 1 | 1.08 |
| 1 月 1 日至 3 月 31 日 | 基线 | 6.405 | 451 | 0 | 78 | 119.60 |
| 1 月 1 日至 3 月 31 日 | 加复合索引 | 5.417 | 348 | 0 | 78 | 119.60 |

**订单侧。** 两个窗口原先都通过 `Index Scan using ix_orders_source_id` 找到快照中的 8,000 行，再按地区、状态过滤掉 7,765 行，得到 235 行，命中 108 页。新增索引支持三个等值条件，也包含联接和订单去重所需的 `id`。计划变为 `Index Only Scan using ix_orders_source_region_status_id`，同样返回 235 行，命中 5 页，`Heap Fetches = 0`。

**退款侧。** 单日通过 `Bitmap Index Scan` 和 `Bitmap Heap Scan` 取出所有快照在当天的 512 条退款，命中 330 页。与 235 笔候选订单做 `Hash Join` 后剩 1 条。季度条件覆盖全部退款，优化器选择 `Seq Scan` 读取 46,544 行，命中 343 页；联接后剩 95 条，聚合得到 78 笔不同订单。两种窗口均在联接结果上排序后做去重聚合。

**成本与适用范围。** 新索引占 5,324,800 字节（约 5.08 MiB），后续导入订单需要维护额外的 B-tree。测试在装载后执行 `VACUUM (ANALYZE)`，这些订单页已全部可见，索引扫描可跳过订单堆表。刚写入的数据可能发生回表。地区分布、退款日期选择性、并发负载和缓存温度会影响执行计划和耗时，本次结果适用于以上固定查询和样本。

## 复现

在项目根目录使用已安装项目依赖的 Python 环境，并将 `PG_BIN` 指向包含 `initdb` 和 `pg_ctl` 的 PostgreSQL 17 二进制目录。以可运行 `initdb` 的普通用户执行以下命令。

```bash
PG_BIN="$(pg_config --bindir)" .venv/bin/python scripts/postgres_smoke.py --benchmark > /tmp/refund-plans.log
rg '^(PostgreSQL|orders |refund_events |Index bytes|SUMMARY)' /tmp/refund-plans.log
```

入口脚本创建私有 Unix 套接字和临时集群，冒烟测试后新建独立数据库 `refund_benchmark`。它按当前模型建表，在基线阶段移除本次比较的索引，装载固定数据，以 `default_statistics_target = 1000` 更新统计信息。脚本逐场景校验应用值与按生成规则独立计算的预期值，最后再次比较索引前后结果。完整 SQL、绑定参数及四份中位数执行计划保存在指定日志中，集群在脚本结束时清理。生成器和采样代码见 [pg_refund_benchmark.py](scripts/pg_refund_benchmark.py)。
