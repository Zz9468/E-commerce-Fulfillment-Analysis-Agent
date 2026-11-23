from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import dict_row

from app.import_data import import_dataset
from app.main import create_app
from app.queries import compare_delivery, compare_delivery_stages

REFERENCE_SQL = Path(__file__).resolve().parents[2] / "eval" / "reference_sql"


def test_olist_matches_reference_sql(
    db: Connection,
    migrated_database_url: str,
    query_database_urls: dict[str, str],
    pytestconfig: pytest.Config,
) -> None:
    directory = pytestconfig.getoption("--olist-dir")
    if directory is None:
        pytest.skip("Pass --olist-dir to verify the original Olist CSVs.")
    orders = Path(directory) / "olist_orders_dataset.csv"
    customers = Path(directory) / "olist_customers_dataset.csv"
    dataset = import_dataset(migrated_database_url, orders, customers, source="olist")
    assert dataset.order_count == 99_441
    assert db.execute(
        """
        SELECT orders_sha256, customers_sha256, has_carrier_handoff
        FROM dataset_version WHERE dataset_id = %s
        """,
        (dataset.dataset_id,),
    ).fetchone() == (
        sha256(orders.read_bytes()).hexdigest(),
        sha256(customers.read_bytes()).hexdigest(),
        True,
    )

    july, august = date(2018, 7, 1), date(2018, 8, 1)
    params = dict(
        dataset_id=dataset.dataset_id,
        months=[july, august],
        month=august,
        baseline_month=july,
    )
    with db.cursor(row_factory=dict_row) as cursor:
        delivery = cursor.execute(
            (REFERENCE_SQL / "delivery.sql").read_text(),
            params,
        ).fetchall()
        stage_reference = cursor.execute(
            (REFERENCE_SQL / "stages.sql").read_text(),
            params,
        ).fetchall()
        concentration = cursor.execute(
            (REFERENCE_SQL / "concentration.sql").read_text(),
            params,
        ).fetchone()
    by_region = {
        (row["region_level"], row["state_code"], row["city_name"], row["month"]): row
        for row in delivery
    }

    kwargs = dict(
        dataset_id=dataset.dataset_id, month="2018-08", baseline_month="2018-07"
    )
    overall = compare_delivery(
        db, metric="late_rate", authorized_state=None, **kwargs
    ).rows[0]
    states = compare_delivery(
        db,
        metric="late_rate",
        group_by="state",
        authorized_state=None,
        **kwargs,
    ).rows
    cities = compare_delivery(
        db,
        metric="late_rate",
        group_by="city",
        authorized_state="SP",
        **kwargs,
    ).rows
    sp = next(row for row in states if row.region == "SP")
    sao_paulo = next(row for row in cities if row.region == "sao paulo")
    for key, result, expected in [
        ((3, None, None), overall, [(208, 6156), (393, 6351)]),
        ((1, "SP", None), sp, [(100, 2712), (285, 3164)]),
        ((0, "SP", "sao paulo"), sao_paulo, [(51, 1059), (124, 1269)]),
    ]:
        for month, stats, counts in zip(
            (july, august), (result.baseline, result.current), expected, strict=True
        ):
            reference = by_region[(*key, month)]
            assert (
                (stats.numerator, stats.sample_count)
                == (
                    reference["late"],
                    reference["late_samples"],
                )
                == counts
            )
            assert stats.value == pytest.approx(reference["late_rate"])
    assert round(overall.delta_pp, 2) == Decimal("2.81")
    assert (
        overall.late_count_delta,
        sp.late_count_delta,
        sao_paulo.late_count_delta,
    ) == (185, 185, 73)

    positive_increments = [
        row.late_count_delta
        for row in states
        if row.region is not None and row.late_count_delta > 0
    ]
    assert max(positive_increments) == concentration["max_positive"] == 185
    assert sum(positive_increments) == concentration["total_positive"] == 233
    assert round(concentration["concentration"] * 100, 1) == Decimal("79.4")

    stages = compare_delivery_stages(db, authorized_state=None, **kwargs)
    assert stages.supported
    assert [row["sample_count"] for row in stage_reference] == [6119, 6306]
    assert [row["excluded_count"] for row in stage_reference] == [37, 45]
    assert [round(row["before_p50"], 2) for row in stage_reference] == [
        Decimal("1.80"),
        Decimal("1.77"),
    ]
    assert [round(row["after_p50"], 2) for row in stage_reference] == [
        Decimal("5.30"),
        Decimal("4.94"),
    ]
    for period, reference in zip(
        (stages.baseline, stages.current), stage_reference, strict=True
    ):
        for name, stage in (
            ("before", period.before_handoff),
            ("after", period.after_handoff),
        ):
            assert stage.sample_count == reference["sample_count"]
            assert stage.excluded_count == reference["excluded_count"]
            assert stage.p50_days == pytest.approx(float(reference[f"{name}_p50"]))
            assert stage.p90_days == pytest.approx(float(reference[f"{name}_p90"]))

    with TestClient(
        create_app(query_database_urls=query_database_urls, mode="rule")
    ) as client:
        payload = {
            "mode": "question",
            "dataset_id": str(dataset.dataset_id),
            "question": "2018年7月和8月超期率变化，排查新增超期集中在哪些地区",
        }
        response = client.post(
            "/api/analysis", json=payload, headers={"Authorization": "Bearer demo-hq"}
        )
    assert response.status_code == 200
    analysis = response.json()
    assert analysis["status"] == "answered"
    assert [step["parameters"]["group_by"] for step in analysis["trace"]] == [
        "none",
        "state",
        "city",
    ]
    assert "79.4%" in analysis["answer"]
    assert "73 单" in analysis["answer"]
    sp_city = next(
        row
        for row in analysis["evidence"][-1]["result"]["rows"]
        if row["region"] == "sao paulo"
    )
    assert (
        sp_city["baseline"]["numerator"],
        sp_city["current"]["numerator"],
        sp_city["late_count_delta"],
    ) == (51, 124, 73)
