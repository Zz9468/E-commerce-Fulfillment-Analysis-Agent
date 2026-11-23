<script setup lang="ts">
import { computed } from 'vue'
import type { Evidence } from '../api/analysis'
import { counts, metricValue, scope, signed } from '../format'

const props = defineProps<{
  evidence: Evidence
  ranking: 'late_count' | 'rate'
  city?: string | null
}>()
const emit = defineEmits<{ drill: [evidenceId: string, state: string] }>()

const delivery = computed(() => ('rows' in props.evidence.result ? props.evidence.result : null))
const stages = computed(() => ('supported' in props.evidence.result ? props.evidence.result : null))
const order = computed(() => {
  const result = delivery.value
  if (!result) return []
  const rows = result.rows.filter((row) => !props.city || row.region === props.city)
  if (!result.baseline_month || result.metric !== 'late_rate') return rows
  const score = (row: (typeof rows)[number]) =>
    Number((props.ranking === 'rate' ? row.delta_pp : row.late_count_delta) ?? -Infinity)
  return rows.sort((a, b) => score(b) - score(a))
})
const steps = [
  ['before_handoff', '下单到交接'],
  ['after_handoff', '交接到签收'],
] as const
</script>

<template>
  <div v-if="delivery && order.length" class="table-scroll">
    <table>
      <thead>
        <tr>
          <th scope="col">{{ delivery.group_by === 'city' ? '城市' : '地区' }}</th>
          <th v-if="delivery.baseline_month" scope="col">{{ delivery.baseline_month }}</th>
          <th scope="col">{{ delivery.month }}</th>
          <th v-if="delivery.baseline_month && delivery.metric === 'late_rate'" scope="col">
            超期率变化
          </th>
          <th v-if="delivery.baseline_month && delivery.metric === 'late_rate'" scope="col">
            超期单数净增
          </th>
          <th v-if="delivery.group_by === 'state'" scope="col">城市</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in order" :key="row.region ?? ''">
          <th scope="row">
            {{
              delivery.group_by === 'none'
                ? scope(delivery.state)
                : row.region
                  ? delivery.group_by === 'state'
                    ? scope(row.region)
                    : row.region
                  : '未知地区'
            }}
          </th>
          <td v-if="row.baseline">
            {{ metricValue(row.baseline.value, delivery.metric) }}
            <small>{{
              row.baseline.numerator === null
                ? `${counts(row.baseline.sample_count)} 个样本`
                : `${row.baseline.numerator}/${row.baseline.sample_count}`
            }}</small>
          </td>
          <td>
            {{ metricValue(row.current.value, delivery.metric) }}
            <small>{{
              row.current.numerator === null
                ? `${counts(row.current.sample_count)} 个样本`
                : `${row.current.numerator}/${row.current.sample_count}`
            }}</small>
          </td>
          <td v-if="delivery.baseline_month && delivery.metric === 'late_rate'">
            {{ signed(row.delta_pp, '百分点') }}
          </td>
          <td v-if="delivery.baseline_month && delivery.metric === 'late_rate'">
            {{ signed(row.late_count_delta, '单', true) }}
          </td>
          <td v-if="delivery.group_by === 'state'">
            <button
              v-if="row.region"
              class="text-button"
              type="button"
              :aria-label="`查看 ${row.region} 城市`"
              @click="emit('drill', evidence.evidence_id, row.region)"
            >
              查看 ↗
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <div v-else-if="stages?.supported" class="table-scroll">
    <table>
      <thead>
        <tr>
          <th scope="col">月份</th>
          <th scope="col">阶段</th>
          <th scope="col">P50</th>
          <th scope="col">P90</th>
          <th scope="col">有效样本</th>
          <th scope="col">剔除</th>
        </tr>
      </thead>
      <tbody>
        <template
          v-for="[period, stats] in [
            [stages.baseline_month, stages.baseline],
            [stages.month, stages.current],
          ] as const"
          :key="period ?? ''"
        >
          <tr v-for="[key, label] in stats ? steps : []" :key="key">
            <th scope="row">{{ period }}</th>
            <td>{{ label }}</td>
            <td>{{ metricValue(stats?.[key].p50_days ?? null, 'stages') }}</td>
            <td>{{ metricValue(stats?.[key].p90_days ?? null, 'stages') }}</td>
            <td>{{ counts(stats?.[key].sample_count ?? 0) }}</td>
            <td>{{ counts(stats?.[key].excluded_count ?? 0) }}</td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
  <p v-else class="muted">{{ stages ? '当前版本缺少交接时间列。' : '该范围没有可展示的行。' }}</p>
</template>
