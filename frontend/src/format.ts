import type { Metric, Numeric, Stats } from './api/analysis'

const count = new Intl.NumberFormat('zh-CN')
const decimal = new Intl.NumberFormat('zh-CN', {
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
})
const percent = new Intl.NumberFormat('zh-CN', {
  style: 'percent',
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
})
const states: Record<string, string> = { SP: '圣保罗州', RJ: '里约热内卢州', MG: '米纳斯吉拉斯州' }

export const metricNames: Record<Metric, string> = {
  late_rate: '送达超期率',
  delivered_count: '已送达订单数',
  avg_delivery_days: '平均送达耗时',
  stages: '配送阶段耗时',
}

export const statusNames = {
  answered: '已完成',
  needs_clarification: '待补充条件',
  scope_denied: '权限受限',
  no_data: '无可计算数据',
  unsupported: '数据能力缺失',
  partial: '部分结果',
  unavailable: '执行失败',
}

export const stepNames = {
  ok: '完成',
  invalid: '参数无效',
  scope_denied: '范围受限',
  duplicate: '重复查询',
  failed: '执行失败',
  budget_exhausted: '预算已用完',
}

export function scope(state: string | null) {
  return state ? `${states[state] ? `${states[state]} · ` : ''}${state}` : '全部地区'
}

export function monthName(month: string) {
  const [year, number] = month.split('-')
  return `${year} 年 ${Number(number)} 月`
}

export function counts(value: number) {
  return count.format(value)
}

export function metricValue(value: Numeric | null, metric: Metric) {
  if (value === null) return '—'
  if (metric === 'late_rate') return percent.format(Number(value))
  if (metric === 'delivered_count') return `${counts(Number(value))} 单`
  return `${decimal.format(Number(value))} 天`
}

export function samples(stats: Stats, metric: Metric) {
  if (metric === 'late_rate')
    return `${counts(stats.numerator ?? 0)} 单超期 / ${counts(stats.sample_count)} 个有效样本`
  return `${counts(stats.sample_count)} 个有效样本`
}

export function signed(value: Numeric | null, unit: string, whole = false) {
  if (value === null) return '—'
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${whole ? counts(number) : decimal.format(number)} ${unit}`
}
