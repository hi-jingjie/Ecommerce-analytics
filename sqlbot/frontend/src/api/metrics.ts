import { request } from '@/utils/request'

export type MetricCode = 'payment_amount' | 'paid_orders' | 'average_order_value' | 'refund_amount'
export type Dimension = 'channel' | 'region'
export interface MetricInfo { code: MetricCode; name: string; unit: string; formula: string; description: string }
export interface Binding {
  metric_code: MetricCode
  table_name: string
  grain_field: string
  amount_field: string | null
  time_field: string
  status_field: string
  status_values: string[]
  dimensions: Partial<Record<Dimension, string>>
  timezone: 'Asia/Shanghai'
}
export interface Definition {
  id: string; metric_code: MetricCode; version: number; binding: Binding; created_at: string
}
export interface Intent {
  metric_code: MetricCode; start_date: string; end_date: string
  dimension: Dimension | null; filters: Partial<Record<Dimension, string>>
}
export interface QueryPlan {
  id: string; status: string; intent: Intent; binding: Binding; version: number
  metric: Omit<MetricInfo, 'code'>; sql: string; parameters: Record<string, string>
  rows?: { dimension?: string | null; value: string | number | null }[]
  executed_at?: string
}
export interface SourceTable { name: string; fields: { name: string; type: string }[] }
export interface HistoryItem {
  id: string; created_at: string; status: string; question: string | null
  metric_code: MetricCode | null; version: number | null
  origin: 'manual' | 'natural_language'; duration_ms: number
}
export interface HistoryDetail extends HistoryItem {
  resolution: Record<string, unknown> | null
  events: { stage: string; status: string; at: string; duration_ms: number; message?: string }[]
  error: { stage: string; code: string; message: string } | null
  query: QueryPlan | null
}
export const metricsApi = {
  catalog: () => request.get<{ metrics: MetricInfo[]; timezone: string; can_manage: boolean }>('/metrics/catalog'),
  sources: () => request.get<{ id: number; name: string }[]>('/metrics/sources'),
  metadata: (id: number) => request.get<SourceTable[]>(`/metrics/sources/${id}/metadata`),
  definitions: (id: number) => request.get<Definition[]>('/metrics/definitions', { params: { datasource_id: id } }),
  publish: (id: number, expected_version: number, binding: Binding) =>
    request.post<Definition>('/metrics/definitions', { datasource_id: id, expected_version, binding }),
  resolve: (id: number, question: string) => request.post<{
    status: string; questions: string[]; intent: Intent | null; query_id: string
  }>('/metrics/resolve', { datasource_id: id, question }),
  preview: (id: number, intent: Intent, resolution_id?: string) =>
    request.post<QueryPlan>('/metrics/preview', { datasource_id: id, intent, resolution_id }),
  execute: (id: string) => request.post<QueryPlan>(`/metrics/queries/${id}/execute`, { confirmed: true }),
  history: (id: number, page: number) => request.get<{ items: HistoryItem[]; total: number; page: number; page_size: number }>(
    '/metrics/history', { params: { datasource_id: id, page, page_size: 20 } }),
  historyDetail: (id: string) => request.get<HistoryDetail>(`/metrics/history/${id}`),
}
