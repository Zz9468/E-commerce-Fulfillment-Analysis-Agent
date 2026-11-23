import logging
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

from app.answers import result_status, scope_name
from app.model import Model, ToolCall
from app.queries import (
    DeliveryComparison,
    DeliveryRow,
    GroupBy,
    Metric,
    StageComparison,
)
from app.schemas import AnalysisStatus, EffectiveQuery, Evidence, Ranking, ToolReply

if TYPE_CHECKING:
    from app.analysis import ToolRun

logger = logging.getLogger(__name__)
MIN_LATE_RATE_CHANGE_PP = Decimal("2")
MIN_SAMPLES_PER_PERIOD = 5
STATE_CONCENTRATION = Decimal("0.60")


def _eligible(row: DeliveryRow) -> bool:
    return bool(
        row.baseline
        and min(row.baseline.sample_count, row.current.sample_count)
        >= MIN_SAMPLES_PER_PERIOD
    )


def _rank(row: DeliveryRow, ranking: Ranking) -> int | Decimal:
    return (row.delta_pp if ranking == "rate" else row.late_count_delta) or 0


def evidence_highlight(item: Evidence, ranking: Ranking) -> str:
    result = item.result
    if isinstance(result, StageComparison):
        if result.baseline and result.current and result_status(item) == "answered":
            before = (
                result.baseline.before_handoff.p50_days,
                result.current.before_handoff.p50_days,
            )
            after = (
                result.baseline.after_handoff.p50_days,
                result.current.after_handoff.p50_days,
            )
            return (
                f"{scope_name(result.state)}阶段证据显示交接前中位耗时 {before[0]:.2f} → {before[1]:.2f} 天，"
                f"交接后 {after[0]:.2f} → {after[1]:.2f} 天。归因到承运商需补充标识和轨迹。"
            )
        return (
            "阶段查询缺少可比较样本。"
            if result.supported
            else "当前版本缺少交接时间列。"
        )
    if result.group_by == GroupBy.NONE or result.metric != Metric.LATE_RATE:
        return ""
    candidates = [row for row in result.rows if row.region and _rank(row, ranking) > 0]
    if not candidates:
        return "当前分组没有可排序的正增量。"
    top = max(candidates, key=lambda row: _rank(row, ranking))
    name = scope_name(top.region) if result.group_by == GroupBy.STATE else top.region
    delta = (
        f"超期率上升 {_rank(top, ranking):.2f} 个百分点"
        if ranking == "rate"
        else f"新增超期 {_rank(top, ranking):,} 单"
    )
    small = "两期有效样本较少，作为待核实线索。" if not _eligible(top) else ""
    return f"{name}的{delta}，在当前查询的已知地区中居首。{small}"


def rule_investigation(
    run: "ToolRun", query: EffectiveQuery, question: str
) -> tuple[AnalysisStatus, str]:
    overall = run.query(query.model_copy(update={"group_by": GroupBy.NONE}))
    if not overall.evidence:
        return "unavailable", "整体查询没有取得可用证据。"
    status = result_status(overall.evidence)
    if status != "answered":
        return status, "两期整体样本补齐后可继续调查。"
    if query.metric != Metric.LATE_RATE or not isinstance(
        overall.evidence.result, DeliveryComparison
    ):
        return "answered", "已完成所选指标的整体比较。"
    row = overall.evidence.result.rows[0]
    if not _eligible(row):
        return (
            "partial",
            f"每期至少需要 {MIN_SAMPLES_PER_PERIOD} 个有效样本来继续规则调查。",
        )
    if row.delta_pp is None or row.delta_pp < MIN_LATE_RATE_CHANGE_PP:
        return (
            "answered",
            f"超期率升幅低于 {MIN_LATE_RATE_CHANGE_PP} 个百分点，规则调查在整体查询后结束。",
        )

    prefer_stages = any(word in question for word in ("环节", "链路", "哪个阶段"))
    if query.state and not prefer_stages:
        target = query.model_copy(update={"group_by": GroupBy.CITY})
    else:
        target = query.model_copy(update={"metric": "stages", "group_by": GroupBy.NONE})
    notes = []
    if not query.state and not prefer_stages:
        by_state = run.query(query.model_copy(update={"group_by": GroupBy.STATE}))
        if not by_state.evidence or not isinstance(
            by_state.evidence.result, DeliveryComparison
        ):
            return "partial", "整体变化已核实，州拆分暂不可用。"
        notes.append(evidence_highlight(by_state.evidence, query.ranking))
        rows = by_state.evidence.result.rows
        unknown = next(
            (row.late_count_delta for row in rows if row.region is None), None
        )
        if unknown:
            notes.append(f"未知地区的超期单数净增为 {unknown:+,} 单。")
        positive = [
            row for row in rows if row.region and (row.late_count_delta or 0) > 0
        ]
        if positive:
            top = max(positive, key=lambda row: row.late_count_delta or 0)
            share = Decimal(top.late_count_delta or 0) / sum(
                row.late_count_delta or 0 for row in positive
            )
            notes.append(f"{scope_name(top.region)}占已知州正增量的 {share:.1%}。")
            if share >= STATE_CONCENTRATION and _eligible(top):
                target = query.model_copy(
                    update={"group_by": GroupBy.CITY, "state": top.region}
                )

    detail = run.query(target)
    if not detail.evidence:
        return "partial", "".join(notes) + "后续查询暂不可用。"
    notes.append(evidence_highlight(detail.evidence, query.ranking))
    if result_status(detail.evidence) != "answered":
        return "partial", "".join(notes)
    return "answered", "".join(notes)


def model_investigation(
    run: "ToolRun", query: EffectiveQuery, question: str, model: Model
) -> tuple[AnalysisStatus, str]:
    run.restrict_to = query
    replies: list[tuple[ToolCall, ToolReply]] = []
    selected = ()
    failed = False
    for turn in range(4):
        try:
            decision = model.choose(
                query, question, replies, tools_enabled=bool(run.remaining)
            )
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
        ) as exc:
            logger.warning(
                "analysis_id=%s model_error=%s", run.analysis_id, type(exc).__name__
            )
            failed = True
            break
        if not run.remaining or not decision.calls:
            selected = decision.evidence_ids
            break
        for call in decision.calls:
            reply = run.execute(call.name, call.arguments)
            replies.append((replace(call, turn=turn), reply))
            if reply.status in {"duplicate", "failed"}:
                failed = True
                break
        if failed:
            break

    usable = [
        item for item in run.evidence if result_status(item) in {"answered", "partial"}
    ]
    primary = next(
        (
            item
            for item in run.evidence
            if isinstance(item.result, DeliveryComparison)
            and item.result.metric == query.metric
            and item.result.group_by == GroupBy.NONE
            and item.result.state == query.state
        ),
        None,
    )
    if primary and query.metric != Metric.LATE_RATE:
        return result_status(primary), ""
    if not usable:
        if primary and result_status(primary) == "no_data" and not failed:
            return "no_data", ""
        return "unavailable", "模型调查未取得可支持答复的证据。"
    focus = [item for item in usable if item.evidence_id in selected] or usable
    notes = "".join(evidence_highlight(item, query.ranking) for item in focus)
    if not primary or result_status(primary) != "answered":
        return "partial", notes + "待补两期整体比较。"
    if run.evidence[0] != primary:
        run.evidence.remove(primary)
        run.evidence.insert(0, primary)
    row = primary.result.rows[0]
    mild = (
        _eligible(row)
        and row.delta_pp is not None
        and row.delta_pp < MIN_LATE_RATE_CHANGE_PP
    )
    detail = any(
        result_status(item) == "answered"
        and (
            isinstance(item.result, StageComparison)
            or (
                isinstance(item.result, DeliveryComparison)
                and item.result.group_by == GroupBy.CITY
                and item.result.metric == query.metric
            )
        )
        for item in usable
    )
    if detail or (mild and not failed):
        return "answered", notes or "整体变化较小，调查已结束。"
    return "partial", notes + "本轮证据支持整体比较，待补地区或阶段明细。"
