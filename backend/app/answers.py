from app.conditions import STATE_NAMES
from app.queries import DeliveryComparison, Metric, StageComparison
from app.schemas import AnalysisStatus, EffectiveQuery, Evidence

METRIC_NAMES = {
    Metric.LATE_RATE: "送达超期率",
    Metric.DELIVERED_COUNT: "已送达订单数",
    Metric.AVG_DELIVERY_DAYS: "平均送达耗时",
    "stages": "配送阶段耗时",
}
FIELD_NAMES = {
    "year": "年份",
    "month": "目标月份",
    "metric": "履约指标",
    "baseline_month": "基准月份",
    "state": "收货州",
}


def scope_name(state: str | None) -> str:
    return STATE_NAMES.get(state, state) if state else "全部地区"


def result_status(item: Evidence, city: str | None = None) -> AnalysisStatus:
    result = item.result
    if isinstance(result, StageComparison):
        if not result.supported:
            return "unsupported"
        has_current = bool(
            result.current and result.current.before_handoff.sample_count
        )
        has_baseline = bool(
            result.baseline and result.baseline.before_handoff.sample_count
        )
    else:
        rows = tuple(row for row in result.rows if city is None or row.region == city)
        has_current = any(row.current.value is not None for row in rows)
        has_baseline = any(
            row.baseline is not None and row.baseline.value is not None for row in rows
        )
    if result.baseline_month and has_current != has_baseline:
        return "partial"
    return "answered" if has_current else "no_data"


def answer_for(
    query: EffectiveQuery,
    evidence: list[Evidence],
    status: AnalysisStatus,
    note: str = "",
) -> str:
    periods = (
        f"{query.baseline_month} → {query.month}"
        if query.baseline_month
        else query.month
    )
    description = f"{scope_name(query.state)}{f' · {query.city}' if query.city else ''}，{periods}，{METRIC_NAMES[query.metric]}。"
    sources = [
        f"{FIELD_NAMES[key]}{'沿用会话' if source == 'session' else '使用页面默认值'}"
        for key, source in query.condition_sources.items()
        if source in {"session", "default"}
    ]
    if query.condition_sources.get("baseline_month") == "previous_month":
        sources.append("基准月份按目标月的上一个自然月推算")
    if sources:
        description += "、".join(sources) + "。"
    if status == "unsupported":
        return description + "当前数据版本缺少交接时间列，无法计算阶段耗时。"
    if status == "no_data":
        return description + "所查月份没有可计算样本。"
    if status == "unavailable":
        return description + (note or "本轮没有取得可支持答复的证据。")
    if not evidence:
        return description + note

    item = evidence[0].result
    if isinstance(item, DeliveryComparison):
        rows = [
            row for row in item.rows if query.city is None or row.region == query.city
        ]
        if rows and (rows[0].region is None or query.city):
            row = rows[0]
            values = []
            for month, stats in (
                (item.baseline_month, row.baseline),
                (item.month, row.current),
            ):
                if stats is None:
                    continue
                value = "无可计算数据"
                if stats.value is not None:
                    if item.metric == Metric.LATE_RATE:
                        value = f"{stats.value:.2%}（{stats.numerator:,}/{stats.sample_count:,}）"
                    elif item.metric == Metric.DELIVERED_COUNT:
                        value = f"{stats.value:,} 单"
                    else:
                        value = (
                            f"{stats.value:.2f} 天（{stats.sample_count:,} 个有效样本）"
                        )
                values.append(f"{month} 为 {value}")
            if row.delta_pp is not None:
                values.append(f"变化 {row.delta_pp:+.2f} 个百分点")
            elif row.delta is not None:
                unit = "单" if item.metric == Metric.DELIVERED_COUNT else "天"
                delta = f"{row.delta:+,}" if unit == "单" else f"{row.delta:+.2f}"
                values.append(f"变化 {delta} {unit}")
            description += "，".join(values) + "。"
        else:
            description += "地区明细见查询证据。"
    else:
        values = []
        for month, stages in (
            (item.baseline_month, item.baseline),
            (item.month, item.current),
        ):
            if stages is None:
                continue
            before, after = stages.before_handoff, stages.after_handoff
            if before.p50_days is not None and after.p50_days is not None:
                values.append(
                    f"{month} 交接前中位数 {before.p50_days:.2f} 天，交接后 {after.p50_days:.2f} 天（{before.sample_count:,} 个有效样本）"
                )
        if values:
            description += "。".join(values) + "。"
    if status == "partial":
        description += "已保留可核实的证据，部分周期或调查步骤缺少可用结果。"
    return description + note
