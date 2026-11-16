<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { metricsApi, type HistoryItem, type HistoryDetail } from '@/api/metrics'

const props = defineProps<{ datasourceId: number }>()
const items = ref<HistoryItem[]>([])
const page = ref(1)
const total = ref(0)
const loading = ref(false)
const error = ref('')
const drawer = ref(false)
const detail = ref<HistoryDetail>()
const detailError = ref('')
const detailLoading = ref(false)
let listRequest = 0
let detailRequest = 0
let active = true
onBeforeUnmount(() => { active = false })
const statusNames: Record<string, string> = { resolving: '解析中', ready: '待预览', needs_clarification: '待澄清',
  preview: '待确认', completed: '已完成', failed: '失败' }
const stageNames: Record<string, string> = { resolve: '问题解析', preview: '查询预览', execute: '确认执行' }
const eventNames: Record<string, string> = { success: '成功', failed: '失败', needs_clarification: '需要澄清' }
const metricNames: Record<string, string> = { payment_amount: '支付金额', paid_orders: '支付订单数',
  average_order_value: '客单价', refund_amount: '退款金额' }
function timestamp(value: string) {
  return new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}
function message(e: unknown) {
  const failure = e as { response?: { data?: { detail?: unknown } }; message?: string; msg?: string }
  const value = failure.response?.data?.detail
  return typeof value === 'string' ? value : failure.msg || failure.message || '读取失败，请刷新重试'
}
async function load() {
  const requestId = ++listRequest
  const source = props.datasourceId
  loading.value = true
  error.value = ''
  try {
    const result = await metricsApi.history(source, page.value)
    if (!active || source !== props.datasourceId || requestId !== listRequest) return
    items.value = result.items
    total.value = result.total
  } catch (e) {
    if (active && requestId === listRequest) { error.value = message(e); items.value = []; total.value = 0 }
  } finally { if (active && requestId === listRequest) loading.value = false }
}
async function inspect(row: HistoryItem) {
  const requestId = ++detailRequest
  const source = props.datasourceId
  detail.value = undefined
  detailError.value = ''
  drawer.value = true
  detailLoading.value = true
  try {
    const value = await metricsApi.historyDetail(row.id)
    if (active && source === props.datasourceId && requestId === detailRequest) detail.value = value
  } catch (e) {
    if (active && requestId === detailRequest) detailError.value = message(e)
  } finally { if (active && requestId === detailRequest) detailLoading.value = false }
}
onMounted(load)
</script>

<template>
  <section class="history-panel" aria-label="查询历史">
    <div class="history-heading"><div><h2>查询历史</h2><p>仅显示你在当前工作空间和数据源的记录。时间按上海时区显示。</p></div>
      <el-button :loading="loading" @click="load">刷新历史</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table v-loading="loading" :data="items" empty-text="暂无查询记录">
      <el-table-column label="时间" width="180"><template #default="scope">{{ timestamp(scope.row.created_at) }}</template></el-table-column>
      <el-table-column label="问题 / 来源" min-width="240"><template #default="scope">{{ scope.row.question || '手动参数查询' }}</template></el-table-column>
      <el-table-column label="指标口径" width="170"><template #default="scope">{{ metricNames[scope.row.metric_code] || '尚未生成口径' }}{{ scope.row.version ? ` · v${scope.row.version}` : '' }}</template></el-table-column>
      <el-table-column label="状态" width="110"><template #default="scope"><el-tag :type="scope.row.status === 'failed' ? 'danger' : scope.row.status === 'completed' ? 'success' : 'info'">{{ statusNames[scope.row.status] || scope.row.status }}</el-tag></template></el-table-column>
      <el-table-column label="耗时" width="110"><template #default="scope">{{ scope.row.duration_ms }} ms</template></el-table-column>
      <el-table-column label="操作" width="110"><template #default="scope"><el-button link type="primary" @click="inspect(scope.row)">查看详情</el-button></template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load" />
    <el-drawer v-model="drawer" title="查询详情" size="min(760px, 95vw)" destroy-on-close>
      <div v-loading="detailLoading" class="history-detail">
        <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" />
        <template v-if="detail">
          <p class="record-id">记录：{{ detail.id }}</p>
          <h3>{{ detail.question || '手动参数查询' }}</h3>
          <p>{{ timestamp(detail.created_at) }} · {{ statusNames[detail.status] || detail.status }} · {{ detail.duration_ms }} ms</p>
          <el-alert v-if="detail.error" :title="detail.error.message" :description="`${stageNames[detail.error.stage] || detail.error.stage} · ${detail.error.code}`" type="error" :closable="false" />
          <h3>执行过程</h3>
          <el-empty v-if="!detail.events.length" description="这条旧记录没有阶段信息，已保留原查询内容" :image-size="60" />
          <ol class="events"><li v-for="(event, index) in detail.events" :key="index">
            <strong>{{ stageNames[event.stage] || event.stage }} · {{ eventNames[event.status] || event.status }}</strong>
            <p>{{ timestamp(event.at) }} · {{ event.duration_ms }} ms</p><p v-if="event.message">{{ event.message }}</p>
          </li></ol>
          <el-collapse v-if="detail.resolution"><el-collapse-item title="模型解析与用量" name="resolution"><pre>{{ JSON.stringify(detail.resolution, null, 2) }}</pre></el-collapse-item></el-collapse>
          <template v-if="detail.query">
            <h3>{{ detail.query.metric.name }} · v{{ detail.query.version }}</h3>
            <p>{{ detail.query.metric.description }}</p>
            <p>{{ detail.query.intent.start_date }} 至 {{ detail.query.intent.end_date }} · {{ detail.query.metric.formula }}</p>
            <el-collapse><el-collapse-item title="当时的口径、SQL 和参数" name="plan">
              <pre>{{ JSON.stringify(detail.query.binding, null, 2) }}</pre><pre>{{ detail.query.sql }}</pre><pre>{{ JSON.stringify(detail.query.parameters, null, 2) }}</pre>
            </el-collapse-item></el-collapse>
            <el-table v-if="detail.query.rows" :data="detail.query.rows" empty-text="没有符合条件的数据">
              <el-table-column v-if="detail.query.intent.dimension" prop="dimension" :label="detail.query.intent.dimension === 'channel' ? '渠道' : '地区'" />
              <el-table-column :label="`结果（${detail.query.metric.unit}）`"><template #default="scope">{{ scope.row.value === null ? '—（无订单，无法计算）' : scope.row.value }}</template></el-table-column>
            </el-table>
            <p v-if="detail.query.executed_at">执行时间：{{ timestamp(detail.query.executed_at) }}</p>
          </template>
          <p class="hint">历史记录仅供查看。需要重新查询时，请回到查询页重新预览并确认。旧记录无阶段信息时耗时显示为 0。</p>
        </template>
      </div>
    </el-drawer>
  </section>
</template>

<style scoped>
.history-panel { padding: 24px; margin: 16px 0; background: white; border: 1px solid #e2e8f0; border-radius: 10px; }
.history-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
h2 { margin: 0; font-size: 18px; } h3 { margin: 24px 0 12px; }
p, .hint { line-height: 1.7; color: #64748b; } .record-id { overflow-wrap: anywhere; font-size: 12px; }
.el-pagination { margin-top: 20px; } .history-detail { min-height: 120px; }
.events { padding-left: 24px; } .events li { margin: 16px 0; } .events p { margin: 5px 0; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f6f8fa; padding: 14px; border-radius: 6px; font-size: 12px; }
</style>
