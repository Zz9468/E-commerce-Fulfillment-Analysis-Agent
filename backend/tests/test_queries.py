import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import Connection
from psycopg.rows import dict_row

from app import queries

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_SQL = Path(__file__).resolve().parents[2] / "eval" / "reference_sql"


@pytest.fixture
def dataset(db: Connection) -> UUID:
    return db.execute(
        """
        INSERT INTO dataset_version (source, orders_sha256, customers_sha256, has_carrier_handoff)
        VALUES ('test', %s, %s, true) RETURNING dataset_id
        """,
        (uuid4().hex * 2, uuid4().hex * 2),
    ).fetchone()[0]


def insert_order(db: Connection, dataset: UUID, *order: str | None) -> None:
    db.execute(
        """
        INSERT INTO order_fact (
            dataset_id, order_id, order_status, purchased_at, estimated_delivery_date,
            delivered_at, handed_to_carrier_at, state_code, city_name
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (dataset, *order),
    )


@pytest.fixture
def synthetic(db: Connection, dataset: UUID) -> UUID:
    with (FIXTURES / "synthetic_orders.csv").open(newline="", encoding="utf-8") as file:
        for order_id, customer, status, purchase, estimate, delivered, handoff in list(
            csv.reader(file)
        )[1:]:
            city = {"sp": "sao paulo", "rj": "rio de janeiro"}[customer]
            insert_order(
                db,
                dataset,
                order_id,
                status,
                purchase,
                estimate or None,
                delivered or None,
                handoff or None,
                customer.upper(),
                city,
            )
    return dataset


def test_ac02_metrics_match_hand_counts_and_reference(
    db: Connection, synthetic: UUID
) -> None:
    with db.cursor(row_factory=dict_row) as cursor:
        reference = cursor.execute(
            (REFERENCE_SQL / "delivery.sql").read_text(),
            {"dataset_id": synthetic, "months": [date(2018, 7, 1), date(2018, 8, 1)]},
        ).fetchall()
    totals = [row for row in reference if row["region_level"] == 3]
    assert [(row["late"], row["late_samples"]) for row in totals] == [(0, 1), (1, 2)]
    assert totals[1]["delivered"] == 3
    assert float(totals[1]["avg_days"]) == pytest.approx(8 / 3)

    kwargs = dict(
        dataset_id=synthetic,
        month="2018-08",
        baseline_month="2018-07",
        authorized_state=None,
    )
    late = queries.compare_delivery(db, metric="late_rate", **kwargs).rows[0]
    assert (late.current.numerator, late.current.sample_count, late.current.value) == (
        1,
        2,
        Decimal("0.5"),
    )
    assert (
        late.baseline.numerator,
        late.baseline.sample_count,
        late.baseline.value,
    ) == (0, 1, 0)
    assert (late.delta_pp, late.late_count_delta) == (50, 1)

    count = queries.compare_delivery(db, metric="delivered_count", **kwargs).rows[0]
    assert (count.current.value, count.current.sample_count, count.delta) == (3, 3, 2)
    average = queries.compare_delivery(db, metric="avg_delivery_days", **kwargs).rows[0]
    assert average.current.sample_count == totals[1]["avg_samples"] == 3
    assert average.current.value == pytest.approx(totals[1]["avg_days"])
    assert average.baseline.value == 3


def test_ac02_state_union_scope_and_empty_month(
    db: Connection, synthetic: UUID
) -> None:
    kwargs = dict(
        dataset_id=synthetic,
        month="2018-08",
        baseline_month="2018-07",
        metric="late_rate",
    )
    regions = queries.compare_delivery(
        db, group_by="state", authorized_state=None, **kwargs
    ).rows
    assert {row.region for row in regions} == {"SP", "RJ"}
    rj = next(row for row in regions if row.region == "RJ")
    assert rj.current.value == 0
    assert (rj.baseline.sample_count, rj.baseline.numerator, rj.baseline.value) == (
        0,
        0,
        None,
    )
    assert (rj.delta_pp, rj.late_count_delta) == (None, 0)
    sp = queries.compare_delivery(db, group_by="city", authorized_state="SP", **kwargs)
    assert sp.state == "SP"
    assert [
        (row.region, row.current.value, row.current.sample_count) for row in sp.rows
    ] == [
        ("sao paulo", Decimal(1), 1),
    ]
    with pytest.raises(PermissionError):
        queries.compare_delivery(db, state="RJ", authorized_state="SP", **kwargs)
    with pytest.raises(ValueError, match="state"):
        queries.compare_delivery(db, group_by="city", authorized_state=None, **kwargs)

    for metric, expected in (
        ("late_rate", None),
        ("delivered_count", 0),
        ("avg_delivery_days", None),
    ):
        empty = queries.compare_delivery(
            db,
            dataset_id=synthetic,
            month="2018-09",
            metric=metric,
            authorized_state=None,
        ).rows[0]
        assert (empty.current.value, empty.current.sample_count, empty.baseline) == (
            expected,
            0,
            None,
        )


def test_ac02_stage_order_exclusions_and_empty_period(
    db: Connection, synthetic: UUID
) -> None:
    result = queries.compare_delivery_stages(
        db,
        dataset_id=synthetic,
        month="2018-08",
        baseline_month="2018-09",
        authorized_state=None,
    )
    assert result.supported is True
    before, after = result.current.before_handoff, result.current.after_handoff
    assert (
        before.p50_days,
        before.p90_days,
        before.sample_count,
        before.excluded_count,
    ) == (1, 1, 1, 1)
    assert (
        after.p50_days,
        after.p90_days,
        after.sample_count,
        after.excluded_count,
    ) == (4, 4, 1, 1)
    assert result.baseline.before_handoff.p50_days is None
    assert result.baseline.after_handoff.sample_count == 0


def test_ac02_continuous_stage_percentiles_match_reference(
    db: Connection, dataset: UUID
) -> None:
    insert_order(
        db,
        dataset,
        "first",
        "delivered",
        "2018-08-01",
        "2018-08-05",
        "2018-08-04",
        "2018-08-02",
        "SP",
        "sao paulo",
    )
    insert_order(
        db,
        dataset,
        "second",
        "delivered",
        "2018-08-01",
        "2018-08-15",
        "2018-08-10",
        "2018-08-04",
        "SP",
        "sao paulo",
    )
    with db.cursor(row_factory=dict_row) as cursor:
        reference = cursor.execute(
            (REFERENCE_SQL / "stages.sql").read_text(),
            {"dataset_id": dataset, "months": [date(2018, 8, 1)], "state": "SP"},
        ).fetchone()
    assert (reference["before_p50"], reference["before_p90"]) == (
        Decimal(2),
        Decimal("2.8"),
    )
    assert (reference["after_p50"], reference["after_p90"]) == (
        Decimal(4),
        Decimal("5.6"),
    )
    result = queries.compare_delivery_stages(
        db,
        dataset_id=dataset,
        month="2018-08",
        authorized_state="SP",
    )
    for name in ("before", "after"):
        stage = getattr(result.current, f"{name}_handoff")
        assert stage.p50_days == pytest.approx(float(reference[f"{name}_p50"]))
        assert stage.p90_days == pytest.approx(float(reference[f"{name}_p90"]))
        assert stage.sample_count == reference["sample_count"] == 2
        assert stage.excluded_count == reference["excluded_count"] == 0


def test_ac02_reversed_or_missing_delivery_time(db: Connection, dataset: UUID) -> None:
    insert_order(
        db,
        dataset,
        "reversed",
        "delivered",
        "2018-08-02 09:00:00",
        "2018-08-01",
        "2018-08-01 19:00:00",
        "2018-08-02 10:00:00",
        "SP",
        "sao paulo",
    )
    insert_order(
        db,
        dataset,
        "missing",
        "delivered",
        "2018-08-31 23:59:59.999999",
        "2018-09-01",
        None,
        None,
        "SP",
        "sao paulo",
    )
    insert_order(
        db,
        dataset,
        "next_month",
        "delivered",
        "2018-09-01 00:00:00",
        "2018-09-01",
        "2018-09-01 12:00:00",
        "2018-09-01 00:00:00",
        "SP",
        "sao paulo",
    )
    kwargs = dict(
        dataset_id=dataset,
        month="2018-08",
        baseline_month="2018-09",
        authorized_state=None,
    )
    late = queries.compare_delivery(db, metric="late_rate", **kwargs).rows[0]
    assert (late.current.numerator, late.current.sample_count, late.current.value) == (
        0,
        1,
        0,
    )
    count = queries.compare_delivery(db, metric="delivered_count", **kwargs).rows[0]
    assert (count.current.value, count.baseline.value) == (2, 1)
    average = queries.compare_delivery(db, metric="avg_delivery_days", **kwargs).rows[0]
    assert (average.current.value, average.current.sample_count) == (None, 0)
    assert (average.baseline.value, average.baseline.sample_count) == (
        Decimal("0.5"),
        1,
    )
    stages = queries.compare_delivery_stages(db, **kwargs)
    assert (
        stages.current.before_handoff.sample_count,
        stages.current.before_handoff.excluded_count,
    ) == (0, 1)
    assert stages.baseline.before_handoff.p50_days == 0
