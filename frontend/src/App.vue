<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { getWorkspace, postAnalysis } from './api/analysis'
import type {
  AccountToken,
  AnalysisRequest,
  AnalysisResponse,
  Defaults,
  Workspace,
} from './api/analysis'
import AnalysisCard from './components/AnalysisCard.vue'
import { counts, metricNames, scope } from './format'

const accounts = { 'demo-hq': '总部运营', 'demo-sp': '圣保罗州运营' } as const
const modes = { rule: '规则调查', fixed: '固定模型重放', model: '模型调查' }
const presets = [
  ['查一个数字', '2018 年 8 月超期率是多少？'],
  ['比较两个月', '2018 年 8 月相比 7 月超期率变差了吗？'],
  ['排查超期集中地区', '2018 年 7 月和 8 月超期率，排查新增超期集中在哪些地区。'],
  ['查看配送阶段', '2018 年 8 月配送阶段耗时'],
] as const

const account = ref<AccountToken>('demo-hq')
const workspace = ref<Workspace | null>(null)
const datasetId = ref('')
const selectedDataset = computed(() =>
  workspace.value?.datasets.find((item) => item.dataset_id === datasetId.value),
)
const defaults = reactive<Defaults>({
  year: 2018,
  month: null,
  baseline_month: null,
  metric: 'late_rate',
})
const years = computed(() => [
  ...new Set(
    (selectedDataset.value?.covered_months ?? []).map((month) => Number(month.slice(0, 4))),
  ),
])
const sessionId = ref<string>()
const question = ref('')
const resultList = ref<{ title: string; reply: AnalysisResponse }[]>([])
const error = ref('')
const loading = ref(false)
const loadingWorkspace = ref(false)
let workspaceRequest: AbortController | undefined
let analysisRequest: AbortController | undefined

function resetSession() {
  analysisRequest?.abort()
  analysisRequest = undefined
  loading.value = false
  sessionId.value = undefined
  resultList.value = []
  error.value = ''
  question.value = ''
}

async function loadWorkspace() {
  workspaceRequest?.abort()
  resetSession()
  workspace.value = null
  datasetId.value = ''
  const request = new AbortController()
  workspaceRequest = request
  loadingWorkspace.value = true
  try {
    const payload = await getWorkspace(account.value, request.signal)
    if (workspaceRequest !== request) return
    workspace.value = payload
    datasetId.value = payload.default_dataset_id ?? payload.datasets[0]?.dataset_id ?? ''
  } catch (failure) {
    if (workspaceRequest === request) error.value = (failure as Error).message
  } finally {
    if (workspaceRequest === request) loadingWorkspace.value = false
  }
}

watch(account, loadWorkspace, { immediate: true, flush: 'sync' })
watch(
  datasetId,
  () => {
    resetSession()
    defaults.year = years.value.includes(2018) ? 2018 : (years.value.at(-1) ?? null)
    defaults.month = null
    defaults.baseline_month = null
  },
  { flush: 'sync' },
)

async function run(request: AnalysisRequest, title: string) {
  if (!datasetId.value) return
  analysisRequest?.abort()
  const controller = new AbortController()
  analysisRequest = controller
  loading.value = true
  error.value = ''
  try {
    const reply = await postAnalysis(account.value, request, controller.signal)
    if (analysisRequest !== controller) return
    if (reply.session_id) sessionId.value = reply.session_id
    resultList.value.unshift({ title, reply })
  } catch (failure) {
    if (analysisRequest === controller) error.value = (failure as Error).message
  } finally {
    if (analysisRequest === controller) loading.value = false
  }
}

function submit() {
  const prompt = question.value.trim()
  if (!prompt || loading.value) return
  question.value = ''
  void run(
    {
      mode: 'question',
      question: prompt,
      session_id: sessionId.value,
      dataset_id: sessionId.value ? undefined : datasetId.value,
      ui_defaults: { ...defaults },
    },
    prompt,
  )
}

function drill(evidenceId: string, state: string) {
  if (!sessionId.value) return
  void run(
    { mode: 'drilldown', session_id: sessionId.value, source_evidence_id: evidenceId, state },
    `${scope(state)}城市明细`,
  )
}

function dataName(source: string) {
  return source === 'synthetic' ? '合成数据' : source === 'olist' ? 'Olist 历史数据' : source
}

onUnmounted(() => {
  workspaceRequest?.abort()
  analysisRequest?.abort()
})
</script>

<template>
  <div class="workspace">
    <aside class="sidebar" aria-label="分析设置">
      <div class="brand">
        <span class="brand-mark" aria-hidden="true"><i /><i /><i /></span>
        <span>电商运营<small>配送时效与地区调查</small></span>
      </div>

      <div class="sidebar-config">
        <label for="account">演示账号</label>
        <select id="account" v-model="account">
          <option v-for="(name, token) in accounts" :key="token" :value="token">{{ name }}</option>
        </select>
        <p v-if="workspace" class="account-scope">
          可查范围 · {{ scope(workspace.account.state) }}
        </p>

        <label for="dataset">数据版本</label>
        <select id="dataset" v-model="datasetId" :disabled="!workspace?.datasets.length">
          <option v-if="!workspace?.datasets.length" value="">
            {{ loadingWorkspace ? '正在读取数据版本' : '暂无可用数据' }}
          </option>
          <option
            v-for="dataset in workspace?.datasets"
            :key="dataset.dataset_id"
            :value="dataset.dataset_id"
          >
            {{ dataName(dataset.source) }} · {{ counts(dataset.order_count) }} 单
          </option>
        </select>
        <dl v-if="selectedDataset" class="dataset-meta">
          <div>
            <dt>覆盖月份</dt>
            <dd>
              {{ selectedDataset.covered_months[0] ?? '无'
              }}<template v-if="selectedDataset.covered_months.length > 1">
                至 {{ selectedDataset.covered_months.at(-1) }}</template
              >
            </dd>
          </div>
          <div>
            <dt>版本 ID</dt>
            <dd class="version" :title="selectedDataset.dataset_id">
              {{ selectedDataset.dataset_id }}
            </dd>
          </div>
          <div>
            <dt>阶段数据</dt>
            <dd>{{ selectedDataset.has_carrier_handoff ? '含交接时间' : '缺少交接时间' }}</dd>
          </div>
        </dl>
      </div>

      <div class="presets">
        <h2>示例问题</h2>
        <button
          v-for="[label, prompt] in presets"
          :key="label"
          type="button"
          @click="question = prompt"
        >
          {{ label }}<span aria-hidden="true">↗</span>
        </button>
      </div>
    </aside>

    <main class="main-content">
      <header class="page-header">
        <div>
          <h1>履约分析工作台</h1>
          <p>按下单月份和收货地区查询配送表现</p>
        </div>
        <button class="secondary-button" type="button" @click="resetSession">＋ 新会话</button>
      </header>

      <form class="question-card" @submit.prevent="submit">
        <div class="question-heading">
          <label for="question">分析问题</label>
          <span v-if="workspace" class="mode"
            ><i aria-hidden="true" />{{ modes[workspace.mode] }}</span
          >
        </div>
        <textarea
          id="question"
          v-model="question"
          rows="3"
          maxlength="2000"
          :placeholder="
            resultList[0]?.reply.status === 'needs_clarification'
              ? '补充刚才缺少的条件…'
              : '例如，2018 年 8 月超期率比 7 月高多少？'
          "
          @keydown.ctrl.enter.prevent="submit"
        />
        <div class="form-footer">
          <fieldset class="defaults">
            <legend>页面默认条件</legend>
            <label
              >年份<select v-model="defaults.year">
                <option :value="null">待确认</option>
                <option v-for="year in years" :key="year" :value="year">{{ year }} 年</option>
              </select></label
            >
            <label
              >目标月<select v-model="defaults.month">
                <option :value="null">待确认</option>
                <option v-for="month in 12" :key="month" :value="month">{{ month }} 月</option>
              </select></label
            >
            <label
              >基准月<select v-model="defaults.baseline_month">
                <option :value="null">待确认</option>
                <option
                  v-for="month in selectedDataset?.covered_months"
                  :key="month"
                  :value="month"
                >
                  {{ month }}
                </option>
              </select></label
            >
            <label
              >指标<select v-model="defaults.metric">
                <option :value="null">待确认</option>
                <option v-for="(name, key) in metricNames" :key="key" :value="key">
                  {{ name }}
                </option>
              </select></label
            >
          </fieldset>
          <button
            class="primary-button"
            type="submit"
            :disabled="loading || !datasetId || !question.trim()"
          >
            {{ loading ? '分析中…' : '开始分析' }} <span aria-hidden="true">→</span>
          </button>
        </div>
      </form>

      <p v-if="error" class="notice error" role="alert">
        {{ error }}
        <button v-if="!workspace" class="text-button" type="button" @click="loadWorkspace">
          重试
        </button>
      </p>
      <p v-else-if="loading" class="notice" role="status">正在查询订单与整理证据…</p>
      <div v-else-if="!resultList.length && workspace && !selectedDataset" class="notice">
        请导入合成数据或 Olist 后刷新页面。
      </div>

      <section v-if="resultList.length" class="results" aria-label="分析结果" aria-live="polite">
        <AnalysisCard
          v-for="({ title, reply }, index) in resultList"
          :key="reply.analysis_id"
          :title="title"
          :reply="reply"
          :active="index === 0"
          @drill="drill"
        />
      </section>
      <section
        v-else-if="!loading && !error && selectedDataset"
        class="empty-results"
        aria-label="分析结果"
      >
        <span class="empty-icon" aria-hidden="true"><i /><i /><i /></span>
        <h2>分析结果</h2>
        <p>提交问题后查看统计结果和实际查询证据。</p>
      </section>
    </main>
  </div>
</template>
