<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus-secondary'
import HistoryPanel from './HistoryPanel.vue'
import { metricsApi, type Binding, type Definition, type Dimension, type Intent, type MetricCode,
  type MetricInfo, type QueryPlan, type SourceTable } from '@/api/metrics'

const catalog = ref<MetricInfo[]>([])
const sources = ref<{ id: number; name: string }[]>([])
const sourceId = ref<number>()
const tables = ref<SourceTable[]>([])
const definitions = ref<Definition[]>([])
const canManage = ref(false)
const busy = ref(false)
const tab = ref('query')
const error = ref('')
const question = ref('上周各渠道的支付金额是多少？')
const questions = ref<string[]>([])
const plan = ref<QueryPlan>()
const dates = ref<[string, string]>()
const metricCode = ref<MetricCode>('payment_amount')
const dimension = ref<Dimension | ''>('')
const channel = ref('')
const region = ref('')
const selectedCode = ref<MetricCode>('payment_amount')
const statusText = ref('paid,refunded')
const channelField = ref('')
const regionField = ref('')
const editor = reactive<Binding>({ metric_code: 'payment_amount', table_name: '', grain_field: '',
  amount_field: null, time_field: '', status_field: '', status_values: [], dimensions: {}, timezone: 'Asia/Shanghai' })
const currentDefinitions = computed(() => {
  const seen = new Set<string>()
  return definitions.value.filter((item) => {
    if (seen.has(item.metric_code)) return false
    seen.add(item.metric_code)
    return true
  })
})
const activeDefinition = computed(() => currentDefinitions.value.find((item) => item.metric_code === metricCode.value))
const latestEditor = computed(() => currentDefinitions.value.find((item) => item.metric_code === selectedCode.value))
const fields = computed(() => tables.value.find((table) => table.name === editor.table_name)?.fields || [])
const editedMetric = computed(() => catalog.value.find((item) => item.code === selectedCode.value))
const dimensionNames: Record<Dimension, string> = { channel: '渠道', region: '地区' }

function showError(e: unknown) {
  const failure = e as { response?: { data?: { detail?: unknown } }; message?: string; msg?: string }
  const detail = failure.response?.data?.detail
  error.value = typeof detail === 'string' ? detail : failure.msg || failure.message || '操作失败，请检查参数后重试'
}
async function action(work: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try { await work() } catch (e) { showError(e) } finally { busy.value = false }
}
function loadEditor() {
  const binding = latestEditor.value?.binding
  Object.assign(editor, binding ? JSON.parse(JSON.stringify(binding)) : { metric_code: selectedCode.value,
    table_name: '', grain_field: '', amount_field: null, time_field: '', status_field: '', status_values: [],
    dimensions: {}, timezone: 'Asia/Shanghai' })
  statusText.value = binding?.status_values.join(',') || (selectedCode.value === 'refund_amount' ? 'success' : 'paid,refunded')
  channelField.value = binding?.dimensions.channel || ''
  regionField.value = binding?.dimensions.region || ''
}
function clearFields() {
  editor.grain_field = ''; editor.amount_field = null; editor.time_field = ''; editor.status_field = ''
  channelField.value = ''; regionField.value = ''
}
async function reloadSource() {
  plan.value = undefined
  questions.value = []
  definitions.value = []
  tables.value = []
  if (!sourceId.value) return
  await action(async () => {
    const [defs, metadata] = await Promise.all([metricsApi.definitions(sourceId.value!), metricsApi.metadata(sourceId.value!)])
    definitions.value = defs
    tables.value = metadata
    loadEditor()
  })
}
watch(selectedCode, loadEditor)
watch([metricCode, dimension, dates, channel, region, question], () => { plan.value = undefined })
onMounted(() => action(async () => {
  const [info, data] = await Promise.all([metricsApi.catalog(), metricsApi.sources()])
  catalog.value = info.metrics
  canManage.value = info.can_manage
  sources.value = data
  if (data[0]) { sourceId.value = data[0].id; await reloadSource() }
}))

function manualIntent(): Intent {
  if (!dates.value?.[0] || !dates.value?.[1]) throw new Error('请选择统计起止日期')
  const filters: Intent['filters'] = {}
  if (channel.value.trim()) filters.channel = channel.value.trim()
  if (region.value.trim()) filters.region = region.value.trim()
  return { metric_code: metricCode.value, start_date: dates.value[0], end_date: dates.value[1],
    dimension: dimension.value || null, filters }
}
function prepare() {
  return action(async () => {
    plan.value = undefined
    questions.value = []
    plan.value = await metricsApi.preview(sourceId.value!, manualIntent())
  })
}
function resolve() {
  return action(async () => {
    plan.value = undefined
    questions.value = []
    const result = await metricsApi.resolve(sourceId.value!, question.value)
    questions.value = result.questions
    if (result.intent) {
      metricCode.value = result.intent.metric_code
      dates.value = [result.intent.start_date, result.intent.end_date]
      dimension.value = result.intent.dimension || ''
      channel.value = result.intent.filters.channel || ''
      region.value = result.intent.filters.region || ''
      plan.value = await metricsApi.preview(sourceId.value!, result.intent, result.query_id)
    }
  })
}
function execute() {
  return action(async () => { plan.value = await metricsApi.execute(plan.value!.id) })
}
function publish() {
  return action(async () => {
    const binding: Binding = { ...editor, metric_code: selectedCode.value,
      status_values: statusText.value.split(/[,，]/).map((value) => value.trim()).filter(Boolean), dimensions: {} }
    if (channelField.value) binding.dimensions.channel = channelField.value
    if (regionField.value) binding.dimensions.region = regionField.value
    if (!binding.table_name || !binding.grain_field || !binding.time_field || !binding.status_field ||
        !binding.status_values.length || (selectedCode.value !== 'paid_orders' && !binding.amount_field)) {
      throw new Error('请填写表、业务编号、时间、状态及金额字段，并填写成功状态值')
    }
    await metricsApi.publish(sourceId.value!, latestEditor.value?.version || 0, binding)
    plan.value = undefined
    definitions.value = await metricsApi.definitions(sourceId.value!)
    loadEditor()
    ElMessage.success('指标口径已发布，新查询将使用新版本')
  })
}
</script>

<template>
  <main class="metric-workbench" v-loading="busy">
    <header class="page-heading">
      <div><h1>指标问数</h1><p>用明确的业务口径查询电商数据，每次计算都有依据。</p></div>
      <el-select v-model="sourceId" placeholder="选择 PostgreSQL 数据源" style="width: 280px" :disabled="busy" @change="reloadSource">
        <el-option v-for="source in sources" :key="source.id" :label="source.name" :value="source.id" />
      </el-select>
    </header>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="notice" />
    <el-empty v-if="!sources.length && !busy" description="还没有 PostgreSQL 数据源。请先在数据源管理中连接业务数据库并选择数据表。" />
    <template v-if="sourceId">
      <div class="metric-cards">
        <button v-for="metric in catalog" :key="metric.code" class="metric-card" :class="{ selected: metricCode === metric.code }"
          @click="metricCode = metric.code">
          <span class="card-top">{{ metric.name }}<el-tag size="small" :type="currentDefinitions.some(d => d.metric_code === metric.code) ? 'success' : 'info'">
            {{ currentDefinitions.find(d => d.metric_code === metric.code) ? `v${currentDefinitions.find(d => d.metric_code === metric.code)?.version}` : '待配置' }}
          </el-tag></span>
          <span class="formula">{{ metric.formula }}</span><span class="description">{{ metric.description }}</span>
        </button>
      </div>
      <el-tabs v-model="tab">
        <el-tab-pane label="查询指标" name="query">
          <el-alert v-if="!currentDefinitions.length" title="请先在「指标口径」中配置并发布至少一个指标。" type="info" :closable="false" />
          <section class="panel">
            <h2>描述你想查询的数据</h2>
            <div class="ask-row"><el-input v-model="question" aria-label="指标问题" placeholder="例如：上周各渠道的支付金额是多少？" @keyup.enter="!busy && sourceId && currentDefinitions.length && resolve()" />
              <el-button type="primary" :disabled="busy || !question.trim() || !currentDefinitions.length" @click="resolve">解析并预览</el-button></div>
            <p class="hint">遇到不明确的指标或日期时会先向你确认。也可以直接填写下方参数。</p>
            <el-alert v-for="item in questions" :key="item" :title="item" type="warning" :closable="false" class="notice" />
            <el-form label-position="top" class="query-form">
              <el-form-item label="指标"><el-select v-model="metricCode"><el-option v-for="metric in catalog" :key="metric.code" :label="metric.name" :value="metric.code" /></el-select></el-form-item>
              <el-form-item label="统计日期（包含首尾两天）"><el-date-picker v-model="dates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期" /></el-form-item>
              <el-form-item label="分组"><el-select v-model="dimension"><el-option label="总体" value="" /><el-option v-for="(_, key) in activeDefinition?.binding.dimensions || {}" :key="key" :label="dimensionNames[key as Dimension]" :value="key" /></el-select></el-form-item>
              <el-form-item label="渠道筛选（可选）"><el-input v-model="channel" placeholder="填写渠道的精确值" /></el-form-item>
              <el-form-item label="地区筛选（可选）"><el-input v-model="region" placeholder="填写地区的精确值" /></el-form-item>
            </el-form>
            <el-button :disabled="busy || !activeDefinition" @click="prepare">预览查询口径</el-button>
          </section>
          <section v-if="plan" class="panel" aria-label="查询预览">
            <div class="section-heading"><h2>{{ plan.status === 'completed' ? '查询结果' : '请确认本次查询' }}</h2><el-tag>{{ plan.metric.name }} · v{{ plan.version }}</el-tag></div>
            <p>{{ plan.metric.description }}</p>
            <dl class="plan-details">
              <div><dt>统计日期</dt><dd>{{ plan.intent.start_date }} 至 {{ plan.intent.end_date }}（上海时间）</dd></div>
              <div><dt>成功状态</dt><dd>{{ plan.binding.status_values.join('、') }}</dd></div>
              <div><dt>计算公式</dt><dd>{{ plan.metric.formula }} · {{ plan.metric.unit }}</dd></div>
              <div><dt>业务粒度</dt><dd>{{ plan.binding.table_name }} / {{ plan.binding.grain_field }}</dd></div>
              <div><dt>时间字段</dt><dd>{{ plan.binding.time_field }}</dd></div>
              <div><dt>分组 / 筛选</dt><dd>{{ plan.intent.dimension ? dimensionNames[plan.intent.dimension] : '总体' }} / {{ Object.entries(plan.intent.filters).map(([k, v]) => `${dimensionNames[k as Dimension]}=${v}`).join('；') || '无附加筛选' }}</dd></div>
            </dl>
            <el-collapse><el-collapse-item title="查看查询 SQL 和参数" name="sql"><pre>{{ plan.sql }}</pre><pre>{{ JSON.stringify(plan.parameters, null, 2) }}</pre></el-collapse-item></el-collapse>
            <el-button v-if="plan.status === 'preview'" type="primary" :disabled="busy" class="confirm" @click="execute">确认口径并执行</el-button>
            <template v-if="plan.rows">
              <el-table :data="plan.rows" empty-text="该范围没有符合条件的数据">
                <el-table-column v-if="plan.intent.dimension" prop="dimension" :label="dimensionNames[plan.intent.dimension]"><template #default="scope">{{ scope.row.dimension ?? '未填写' }}</template></el-table-column>
                <el-table-column :label="`${plan.metric.name}（${plan.metric.unit}）`"><template #default="scope">{{ scope.row.value === null ? '—（无订单，无法计算）' : scope.row.value }}</template></el-table-column>
              </el-table>
              <p class="hint">执行时间：{{ plan.executed_at }} · 查询记录：{{ plan.id }}</p>
            </template>
          </section>
        </el-tab-pane>
        <el-tab-pane label="指标口径" name="definitions">
          <section class="panel">
            <h2>配置指标的数据来源</h2><p class="hint">绑定每个订单或每笔退款只有一行的表或视图。金额单位为元，需使用同一币种；支付金额不扣退款。</p>
            <el-form label-position="top" class="binding-form">
              <el-form-item label="指标"><el-select v-model="selectedCode"><el-option v-for="metric in catalog" :key="metric.code" :label="metric.name" :value="metric.code" /></el-select></el-form-item>
              <el-form-item label="数据表 / 视图"><el-select v-model="editor.table_name" :disabled="!canManage" @change="clearFields"><el-option v-for="table in tables" :key="table.name" :label="table.name" :value="table.name" /></el-select></el-form-item>
              <el-form-item :label="selectedCode === 'refund_amount' ? '唯一退款流水编号' : '唯一订单编号'"><el-select v-model="editor.grain_field" :disabled="!canManage"><el-option v-for="field in fields" :key="field.name" :label="field.name" :value="field.name" /></el-select></el-form-item>
              <el-form-item v-if="selectedCode !== 'paid_orders'" label="金额字段（元）"><el-select v-model="editor.amount_field" :disabled="!canManage"><el-option v-for="field in fields" :key="field.name" :label="field.name" :value="field.name" /></el-select></el-form-item>
              <el-form-item :label="selectedCode === 'refund_amount' ? '退款成功时间字段' : '支付时间字段'"><el-select v-model="editor.time_field" :disabled="!canManage"><el-option v-for="field in fields" :key="field.name" :label="field.name" :value="field.name" /></el-select></el-form-item>
              <el-form-item label="状态字段"><el-select v-model="editor.status_field" :disabled="!canManage"><el-option v-for="field in fields" :key="field.name" :label="field.name" :value="field.name" /></el-select></el-form-item>
              <el-form-item label="计入统计的成功状态值（逗号分隔）"><el-input v-model="statusText" :disabled="!canManage" /></el-form-item>
              <el-form-item label="渠道字段（可选）"><el-select v-model="channelField" clearable :disabled="!canManage"><el-option v-for="field in fields" :key="field.name" :label="field.name" :value="field.name" /></el-select></el-form-item>
              <el-form-item label="地区字段（可选）"><el-select v-model="regionField" clearable :disabled="!canManage"><el-option v-for="field in fields" :key="field.name" :label="field.name" :value="field.name" /></el-select></el-form-item>
            </el-form>
            <el-alert :title="`${editedMetric?.formula || ''}；${editedMetric?.description || ''}`" type="info" :closable="false" />
            <p class="hint">发布会保存一个新版本，历史口径保留。请填写数据库中实际使用的状态值，示例 paid、refunded、success 仅供参考。</p>
            <el-button v-if="canManage" type="primary" :disabled="busy" @click="publish">发布 v{{ (latestEditor?.version || 0) + 1 }}</el-button>
            <p v-else class="hint">当前账号可查看口径；发布需要工作空间管理员权限。</p>
          </section>
          <section class="panel"><h2>版本记录</h2><el-table :data="definitions.filter(d => d.metric_code === selectedCode)" empty-text="尚未发布此指标">
            <el-table-column prop="version" label="版本" width="90" /><el-table-column prop="binding.table_name" label="数据表" /><el-table-column prop="binding.time_field" label="时间字段" />
            <el-table-column label="状态值"><template #default="scope">{{ scope.row.binding.status_values.join('、') }}</template></el-table-column>
            <el-table-column prop="created_at" label="发布时间（UTC）" />
          </el-table></section>
        </el-tab-pane>
        <el-tab-pane label="查询历史" name="history">
          <HistoryPanel v-if="tab === 'history' && sourceId" :key="sourceId" :datasource-id="sourceId" />
        </el-tab-pane>
      </el-tabs>
    </template>
  </main>
</template>

<style scoped lang="less">
.metric-workbench { height: 100%; overflow: auto; padding: 28px 36px; background: #f6f8fa; color: #1f2937; }
.page-heading, .section-heading, .card-top { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
h1 { margin: 0; font-size: 25px; } h2 { margin: 0 0 16px; font-size: 17px; }
.page-heading p, .hint, .description { color: #64748b; line-height: 1.7; font-size: 13px; }
.metric-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin: 24px 0; }
.metric-card { cursor: pointer; text-align: left; background: white; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px; font: inherit; }
.metric-card.selected { border-color: #3370ff; box-shadow: 0 0 0 1px #3370ff; }
.card-top { font-weight: 600; gap: 8px; } .formula { display: block; font-size: 14px; margin: 18px 0 10px; }
.description { display: block; }.panel { background: white; border: 1px solid #e2e8f0; border-radius: 10px; padding: 24px; margin: 16px 0; }
.ask-row { display: flex; gap: 12px; }.query-form, .binding-form { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 20px; margin-top: 22px; }
.notice { margin: 12px 0; }.plan-details { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 24px 0; }
dt { color: #64748b; font-size: 12px; margin-bottom: 6px; } dd { margin: 0; word-break: break-word; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: #f6f8fa; padding: 14px; border-radius: 6px; font-size: 12px; line-height: 1.7; }
.confirm { margin-top: 20px; }
@media (max-width: 1100px) { .metric-cards { grid-template-columns: 1fr 1fr; }.query-form, .binding-form { grid-template-columns: 1fr 1fr; } }
@media (max-width: 700px) { .metric-workbench { padding: 16px; }.page-heading, .ask-row { align-items: stretch; flex-direction: column; }.metric-cards, .query-form, .binding-form, .plan-details { grid-template-columns: 1fr; } }
</style>
