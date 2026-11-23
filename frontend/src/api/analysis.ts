export type AccountToken = 'demo-hq' | 'demo-sp'
export type Metric = 'late_rate' | 'delivered_count' | 'avg_delivery_days' | 'stages'
export type Group = 'none' | 'state' | 'city'
export type Numeric = string | number

export interface Dataset {
  dataset_id: string
  source: string
  imported_at: string
  covered_months: string[]
  order_count: number
  has_carrier_handoff: boolean
}

export interface Workspace {
  account: { account_id: AccountToken; display_name: string; state: string | null }
  mode: 'rule' | 'fixed' | 'model'
  default_dataset_id: string | null
  datasets: Dataset[]
}

export interface Defaults {
  year: number | null
  month: number | null
  baseline_month: string | null
  metric: Metric | null
}

export type AnalysisRequest =
  | {
      mode: 'question'
      question: string
      session_id?: string
      dataset_id?: string
      ui_defaults: Defaults
    }
  | { mode: 'drilldown'; session_id: string; source_evidence_id: string; state: string }

export interface Stats {
  value: Numeric | null
  sample_count: number
  numerator: number | null
}

export interface DeliveryRow {
  region: string | null
  current: Stats
  baseline: Stats | null
  delta: Numeric | null
  delta_pp: Numeric | null
  late_count_delta: number | null
}

export interface Comparison {
  dataset_id: string
  month: string
  baseline_month: string | null
  state: string | null
  metric_version: string
}

export interface DeliveryResult extends Comparison {
  metric: Exclude<Metric, 'stages'>
  group_by: Group
  rows: DeliveryRow[]
}

export interface StageStats {
  p50_days: number | null
  p90_days: number | null
  sample_count: number
  excluded_count: number
}

export interface Stages {
  before_handoff: StageStats
  after_handoff: StageStats
}

export interface StageResult extends Comparison {
  supported: boolean
  current: Stages | null
  baseline: Stages | null
}

export interface Evidence {
  evidence_id: string
  analysis_id: string
  tool: string
  completed_at: string
  result: DeliveryResult | StageResult
}

export interface TraceStep {
  sequence: number
  tool: string
  parameters: Record<string, string | null> | null
  status: 'ok' | 'invalid' | 'scope_denied' | 'duplicate' | 'failed' | 'budget_exhausted'
  duration_ms: number
  evidence_id: string | null
}

export interface AnalysisResponse {
  session_id: string
  analysis_id: string
  status:
    | 'answered'
    | 'needs_clarification'
    | 'scope_denied'
    | 'no_data'
    | 'unsupported'
    | 'partial'
    | 'unavailable'
  answer: string
  effective_query:
    | (Comparison & {
        metric: Metric
        group_by: Group
        city: string | null
        ranking: 'late_count' | 'rate'
        condition_sources: Record<string, string>
      })
    | null
  evidence: Evidence[]
  trace: TraceStep[]
  pending_fields: string[]
}

async function call<T>(
  path: string,
  token: AccountToken,
  signal: AbortSignal,
  request?: AnalysisRequest,
): Promise<T> {
  const response = await fetch(path, {
    method: request ? 'POST' : 'GET',
    signal,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(request && { 'Content-Type': 'application/json' }),
    },
    body: request ? JSON.stringify(request) : undefined,
  })
  const body = await response.json().catch(() => null)
  if (!response.ok && !(response.status === 503 && body?.status === 'unavailable')) {
    throw new Error(
      typeof body?.detail === 'string' ? body.detail : '暂时无法连接分析服务，请重试。',
    )
  }
  return body as T
}

export function getWorkspace(token: AccountToken, signal: AbortSignal): Promise<Workspace> {
  return call('/api/workspace', token, signal)
}

export function postAnalysis(
  token: AccountToken,
  request: AnalysisRequest,
  signal: AbortSignal,
): Promise<AnalysisResponse> {
  return call('/api/analysis', token, signal, request)
}
