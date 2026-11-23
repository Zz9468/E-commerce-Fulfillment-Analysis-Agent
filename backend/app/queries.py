"""PostgreSQL delivery metrics shared by the analysis entry points."""

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from psycopg import Connection, sql
from psycopg.rows import tuple_row

DELIVERY_METRIC_VERSION = "1"


class Metric(StrEnum):
    DELIVERED_COUNT = "delivered_count"
    LATE_RATE = "late_rate"
    AVG_DELIVERY_DAYS = "avg_delivery_days"


class GroupBy(StrEnum):
    NONE = "none"
    STATE = "state"
    CITY = "city"


@dataclass(frozen=True)
class MetricStats:
    value: int | Decimal | None
    sample_count: int
    numerator: int | None = None


@dataclass(frozen=True)
class DeliveryRow:
    region: str | None
    current: MetricStats
    baseline: MetricStats | None
    delta: int | Decimal | None
    delta_pp: Decimal | None
    late_count_delta: int | None


@dataclass(frozen=True)
class DeliveryComparison:
    dataset_id: UUID
    month: str
    baseline_month: str | None
    metric: Metric
    group_by: GroupBy
    state: str | None
    rows: tuple[DeliveryRow, ...]
    metric_version: str = DELIVERY_METRIC_VERSION


@dataclass(frozen=True)
class StageStats:
    p50_days: float | None
    p90_days: float | None
    sample_count: int
    excluded_count: int


@dataclass(frozen=True)
class DeliveryStages:
    before_handoff: StageStats
    after_handoff: StageStats


@dataclass(frozen=True)
class StageComparison:
    dataset_id: UUID
    month: str
    baseline_month: str | None
    state: str | None
    supported: bool
    current: DeliveryStages | None
    baseline: DeliveryStages | None
    metric_version: str = DELIVERY_METRIC_VERSION


_REGION = {
    GroupBy.NONE: sql.SQL("NULL::text"),
    GroupBy.STATE: sql.SQL("o.state_code"),
    GroupBy.CITY: sql.SQL("o.city_name"),
}
_LATE_SAMPLE = sql.SQL("""
    o.order_status = 'delivered'
    AND o.estimated_delivery_date IS NOT NULL AND o.delivered_at IS NOT NULL
""")
_DURATION_SAMPLE = sql.SQL(
    "o.order_status = 'delivered' AND o.purchased_at <= o.delivered_at"
)
_METRIC_AGGREGATES = {
    Metric.DELIVERED_COUNT: sql.SQL("""
        count(*) FILTER (WHERE o.order_status = 'delivered') AS sample_count,
        NULL AS numerator, NULL AS avg_days
    """),
    Metric.LATE_RATE: sql.SQL("""
        count(*) FILTER (WHERE {sample}) AS sample_count,
        count(*) FILTER (
            WHERE {sample} AND o.delivered_at::date > o.estimated_delivery_date
        ) AS numerator,
        NULL AS avg_days
    """).format(sample=_LATE_SAMPLE),
    Metric.AVG_DELIVERY_DAYS: sql.SQL("""
        count(*) FILTER (WHERE {sample}) AS sample_count,
        NULL AS numerator,
        avg(extract(epoch FROM (o.delivered_at - o.purchased_at)))
            FILTER (WHERE {sample}) / 86400 AS avg_days
    """).format(sample=_DURATION_SAMPLE),
}

_PERIODS = sql.SQL("""
    WITH periods AS (
        SELECT DISTINCT month
        FROM (VALUES (%(month)s::date), (%(baseline_month)s::date)) AS input(month)
        WHERE month IS NOT NULL
    )
""")
_ORDER_FILTER = sql.SQL("""
    o.dataset_id = %(dataset_id)s
    AND o.purchased_at >= p.month AND o.purchased_at < p.month + interval '1 month'
    AND (%(state)s::text IS NULL OR o.state_code = %(state)s)
""")
_DELIVERY_SQL = sql.SQL("""
    , aggregated AS (
        SELECT p.month, {region} AS region, {aggregates}
        FROM periods p
        JOIN order_fact o ON {order_filter}
        GROUP BY 1, 2
    )
    SELECT a.*
    FROM dataset_version v
    LEFT JOIN aggregated a ON true
    WHERE v.dataset_id = %(dataset_id)s
    ORDER BY a.region NULLS LAST, a.month
""")
_STAGES_SQL = _PERIODS + sql.SQL("""
    , intervals AS (
        SELECT p.month,
               o.handed_to_carrier_at BETWEEN o.purchased_at AND o.delivered_at AS valid,
               extract(epoch FROM o.handed_to_carrier_at - o.purchased_at) / 86400 AS before_days,
               extract(epoch FROM o.delivered_at - o.handed_to_carrier_at) / 86400 AS after_days
        FROM periods p
        JOIN order_fact o ON {order_filter}
        WHERE {late_sample}
          AND EXISTS (
              SELECT 1 FROM dataset_version
              WHERE dataset_id = %(dataset_id)s AND has_carrier_handoff
          )
    )
    SELECT p.month, v.has_carrier_handoff,
           count(*) FILTER (WHERE i.valid) AS sample_count,
           count(i.month) - count(*) FILTER (WHERE i.valid) AS excluded_count,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY i.before_days) FILTER (WHERE i.valid),
           percentile_cont(0.9) WITHIN GROUP (ORDER BY i.before_days) FILTER (WHERE i.valid),
           percentile_cont(0.5) WITHIN GROUP (ORDER BY i.after_days) FILTER (WHERE i.valid),
           percentile_cont(0.9) WITHIN GROUP (ORDER BY i.after_days) FILTER (WHERE i.valid)
    FROM dataset_version v
    CROSS JOIN periods p
    LEFT JOIN intervals i ON i.month = p.month
    WHERE v.dataset_id = %(dataset_id)s
    GROUP BY p.month, v.has_carrier_handoff
""").format(order_filter=_ORDER_FILTER, late_sample=_LATE_SAMPLE)


def _month_start(month: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("month must use YYYY-MM")
    return date.fromisoformat(f"{month}-01")


def _effective_state(authorized_state: str | None, state: str | None) -> str | None:
    for code in (authorized_state, state):
        if code is not None and not re.fullmatch(r"[A-Z]{2}", code):
            raise ValueError("state must be a two-letter uppercase code")
    if authorized_state is not None and state is not None and state != authorized_state:
        raise PermissionError("state is outside the authorized scope")
    return state or authorized_state


def _metric_stats(
    metric: Metric,
    sample_count: int = 0,
    numerator: int | None = None,
    avg_days: Decimal | None = None,
) -> MetricStats:
    if metric == Metric.DELIVERED_COUNT:
        return MetricStats(sample_count, sample_count)
    if metric == Metric.AVG_DELIVERY_DAYS:
        return MetricStats(avg_days, sample_count)
    late_count = numerator or 0
    rate = Decimal(late_count) / sample_count if sample_count else None
    return MetricStats(rate, sample_count, late_count)


def _comparison_row(
    region: str | None,
    current: MetricStats,
    baseline: MetricStats | None,
) -> DeliveryRow:
    delta = delta_pp = late_count_delta = None
    if baseline is not None:
        if current.value is not None and baseline.value is not None:
            delta = current.value - baseline.value
        if current.numerator is not None and baseline.numerator is not None:
            late_count_delta = current.numerator - baseline.numerator
            delta_pp = Decimal(delta) * 100 if delta is not None else None
            delta = None
    return DeliveryRow(region, current, baseline, delta, delta_pp, late_count_delta)


def compare_delivery(
    connection: Connection,
    *,
    dataset_id: UUID,
    month: str,
    metric: Metric | str,
    authorized_state: str | None,
    baseline_month: str | None = None,
    state: str | None = None,
    group_by: GroupBy | str = GroupBy.NONE,
) -> DeliveryComparison:
    """Return one or two months of a delivery metric in a single SQL query."""
    metric, group_by = Metric(metric), GroupBy(group_by)
    state = _effective_state(authorized_state, state)
    if group_by == GroupBy.CITY and state is None:
        raise ValueError("city grouping requires a state")
    target = _month_start(month)
    baseline = _month_start(baseline_month) if baseline_month is not None else None
    statement = _PERIODS + _DELIVERY_SQL.format(
        region=_REGION[group_by],
        aggregates=_METRIC_AGGREGATES[metric],
        order_filter=_ORDER_FILTER,
    )
    with connection.cursor(row_factory=tuple_row) as cursor:
        records = cursor.execute(
            statement,
            dict(
                month=target,
                baseline_month=baseline,
                dataset_id=dataset_id,
                state=state,
            ),
        ).fetchall()
    if not records:
        raise LookupError(f"Unknown dataset version {dataset_id}")

    empty = _metric_stats(metric)
    groups: dict[str | None, dict[date, MetricStats]] = (
        {None: {}} if group_by == GroupBy.NONE else {}
    )
    for period, region, samples, numerator, avg_days in records:
        if period is not None:
            groups.setdefault(region, {})[period] = _metric_stats(
                metric, samples, numerator, avg_days
            )
    rows = tuple(
        _comparison_row(
            region,
            periods.get(target, empty),
            periods.get(baseline, empty) if baseline is not None else None,
        )
        for region, periods in groups.items()
    )
    return DeliveryComparison(
        dataset_id, month, baseline_month, metric, group_by, state, rows
    )


def compare_delivery_stages(
    connection: Connection,
    *,
    dataset_id: UUID,
    month: str,
    authorized_state: str | None,
    baseline_month: str | None = None,
    state: str | None = None,
) -> StageComparison:
    """Compare handoff stages, preserving the dataset's source capability."""
    state = _effective_state(authorized_state, state)
    target = _month_start(month)
    baseline = _month_start(baseline_month) if baseline_month is not None else None
    with connection.cursor(row_factory=tuple_row) as cursor:
        records = cursor.execute(
            _STAGES_SQL,
            dict(
                month=target,
                baseline_month=baseline,
                dataset_id=dataset_id,
                state=state,
            ),
        ).fetchall()
    if not records:
        raise LookupError(f"Unknown dataset version {dataset_id}")
    if not records[0][1]:
        return StageComparison(
            dataset_id, month, baseline_month, state, False, None, None
        )

    periods = {
        period: DeliveryStages(
            StageStats(before_p50, before_p90, samples, excluded),
            StageStats(after_p50, after_p90, samples, excluded),
        )
        for period, _, samples, excluded, before_p50, before_p90, after_p50, after_p90 in records
    }
    return StageComparison(
        dataset_id,
        month,
        baseline_month,
        state,
        True,
        periods[target],
        periods[baseline] if baseline is not None else None,
    )
