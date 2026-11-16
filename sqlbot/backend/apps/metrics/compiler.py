"""No model-authored SQL: only fixed aggregations over validated bindings."""
from dataclasses import dataclass
from datetime import timedelta

from apps.metrics.schemas import MetricBinding, MetricIntent


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    params: dict
    integrity_sql: str


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def required_fields(binding: MetricBinding, intent: MetricIntent) -> set[str]:
    dimensions = set(intent.filters)
    if intent.dimension:
        dimensions.add(intent.dimension)
    if not dimensions.issubset(binding.dimensions):
        raise ValueError('该指标未配置所选维度，请先绑定维度字段')
    fields = {binding.grain_field, binding.time_field, binding.status_field}
    if binding.metric_code != 'paid_orders':
        fields.add(binding.amount_field)
    fields.update(binding.dimensions[key] for key in dimensions)
    return fields


def compile_metric(binding: MetricBinding, intent: MetricIntent, schema: str,
                   row_filter: str = '') -> CompiledQuery:
    if binding.metric_code != intent.metric_code:
        raise ValueError('指标与口径不匹配')
    required_fields(binding, intent)
    q = quote_identifier
    table = f'{q(schema)}.{q(binding.table_name)}'
    # Typed dates become bound midnight strings; PostgreSQL interprets them in
    # the transaction's explicitly set business timezone, including timestamptz.
    params = {'start': f'{intent.start_date} 00:00:00',
              'end': f'{intent.end_date + timedelta(days=1)} 00:00:00'}
    statuses = []
    for i, value in enumerate(binding.status_values):
        key = f'status_{i}'
        params[key] = value
        statuses.append(':' + key)
    predicates = [f'{q(binding.time_field)} >= :start', f'{q(binding.time_field)} < :end',
                  f'{q(binding.status_field)} IN ({", ".join(statuses)})']
    for key, value in intent.filters.items():
        params['filter_' + key] = value
        predicates.append(f'{q(binding.dimensions[key])} = :filter_{key}')
    # This comes only from the existing server-side permission resolver.
    if row_filter:
        predicates.append(f'({row_filter})')
    where = ' AND '.join(predicates)
    amount = q(binding.amount_field) if binding.amount_field else None
    expression = 'COUNT(*)'
    if binding.metric_code in ('payment_amount', 'refund_amount'):
        expression = f'COALESCE(SUM({amount}), 0)'
    elif binding.metric_code == 'average_order_value':
        expression = f'SUM({amount}) * 1.0 / NULLIF(COUNT(*), 0)'
    dimension = q(binding.dimensions[intent.dimension]) if intent.dimension else None
    projection = f'{dimension} AS dimension, ' if dimension else ''
    suffix = f' GROUP BY {dimension} ORDER BY {dimension} LIMIT 1001' if dimension else ''
    null_amount = f'SUM(CASE WHEN {amount} IS NULL THEN 1 ELSE 0 END)' if amount else '0'
    return CompiledQuery(
        sql=f'SELECT {projection}{expression} AS value FROM {table} WHERE {where}{suffix}',
        params=params,
        integrity_sql=f'SELECT COUNT(*) AS row_count, COUNT(DISTINCT {q(binding.grain_field)}) AS grain_count, '
                      f'{null_amount} AS null_amount_count FROM {table} WHERE {where}',
    )
