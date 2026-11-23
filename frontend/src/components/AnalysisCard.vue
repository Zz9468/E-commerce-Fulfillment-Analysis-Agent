<script setup lang="ts">
import { computed } from 'vue'
import type { AnalysisResponse } from '../api/analysis'
import {
  metricNames,
  metricValue,
  monthName,
  samples,
  scope,
  signed,
  statusNames,
  stepNames,
} from '../format'
import EvidenceTable from './EvidenceTable.vue'

const props = defineProps<{ title: string; reply: AnalysisResponse; active: boolean }>()
const emit = defineEmits<{ drill: [evidenceId: string, state: string] }>()

const result = computed(() => props.reply.evidence.find((item) => 'rows' in item.result)?.result)
const primary = computed(() => {
  if (!result.value || !('rows' in result.value) || !props.reply.effective_query) return null
  const { city } = props.reply.effective_query
  return (
    result.value.rows.find((row) =>
      city
        ? row.region === city
        : row.region === null &&
          result.value &&
          'group_by' in result.value &&
          result.value.group_by === 'none',
    ) ?? null
  )
})
const metric = computed(() => props.reply.effective_query?.metric)
const periods = computed(() => {
  const row = primary.value
  const comparison = result.value
  if (!comparison || !row) return []
  return [
    ...(comparison.baseline_month && row.baseline
      ? [{ month: comparison.baseline_month, stats: row.baseline }]
      : []),
    { month: comparison.month, stats: row.current },
  ]
})
const evidenceById = computed(
  () => new Map(props.reply.evidence.map((item) => [item.evidence_id, item])),
)
</script>

<template>
  <article class="analysis-card" :class="{ historical: !active }">
    <header class="result-header">
      <div>
        <p class="eyebrow">{{ active ? '本轮分析' : '会话记录' }}</p>
        <h2>{{ title }}</h2>
      </div>
      <span class="status" :data-status="reply.status">{{ statusNames[reply.status] }}</span>
    </header>
    <p class="answer">{{ reply.answer }}</p>

    <div v-if="reply.effective_query" class="scope-line">
      <span>{{ scope(reply.effective_query.state) }}</span>
      <span
        >{{
          reply.effective_query.baseline_month ? `${reply.effective_query.baseline_month} → ` : ''
        }}{{ reply.effective_query.month }}</span
      >
      <span>{{ metricNames[reply.effective_query.metric] }}</span>
    </div>

    <div v-if="periods.length && metric" class="periods">
      <section v-for="period in periods" :key="period.month" class="period-tile">
        <p>{{ monthName(period.month) }}</p>
        <strong>{{ metricValue(period.stats.value, metric) }}</strong>
        <small>{{ samples(period.stats, metric) }}</small>
        <div
          v-if="metric === 'late_rate' && period.stats.value !== null"
          class="ratio"
          aria-hidden="true"
        >
          <span :style="{ width: `${Number(period.stats.value) * 100}%` }" />
        </div>
      </section>
      <section v-if="primary && periods.length === 2" class="period-tile delta">
        <p>两期变化</p>
        <strong>{{
          metric === 'late_rate'
            ? signed(primary.delta_pp, '百分点')
            : signed(
                primary.delta,
                metric === 'delivered_count' ? '单' : '天',
                metric === 'delivered_count',
              )
        }}</strong>
        <small v-if="primary.late_count_delta !== null"
          >超期单数净增 {{ signed(primary.late_count_delta, '单', true) }}</small
        >
      </section>
    </div>

    <div v-if="reply.trace.length || reply.evidence.length" class="evidence-area">
      <h3>查询证据</h3>
      <ol class="trace-list">
        <li v-for="step in reply.trace" :key="step.sequence">
          <details>
            <summary>
              <span class="step-number">{{ step.sequence }}</span>
              <span class="step-title">{{
                step.tool === 'compare_delivery_stages'
                  ? '配送阶段查询'
                  : step.parameters?.group_by === 'state'
                    ? '州拆分查询'
                    : step.parameters?.group_by === 'city'
                      ? '城市拆分查询'
                      : '指标查询'
              }}</span>
              <span class="step-status"
                >{{ stepNames[step.status] }} · {{ step.duration_ms }} ms</span
              >
            </summary>
            <div class="trace-details">
              <p v-if="step.parameters" class="trace-meta">
                {{ scope(step.parameters.state ?? null) }} ·
                {{ step.parameters.baseline_month ? `${step.parameters.baseline_month} → ` : ''
                }}{{ step.parameters.month }} · {{ step.tool }}
              </p>
              <template v-if="step.evidence_id && evidenceById.has(step.evidence_id)">
                <EvidenceTable
                  :evidence="evidenceById.get(step.evidence_id)!"
                  :ranking="reply.effective_query?.ranking ?? 'late_count'"
                  :city="reply.effective_query?.city"
                  @drill="(id, state) => emit('drill', id, state)"
                />
                <p class="trace-meta evidence-id">
                  口径 v{{ evidenceById.get(step.evidence_id)?.result.metric_version }} · 证据
                  {{ step.evidence_id }}
                </p>
              </template>
            </div>
          </details>
        </li>
      </ol>
    </div>
  </article>
</template>
