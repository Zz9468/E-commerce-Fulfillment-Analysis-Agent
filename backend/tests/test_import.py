import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from threading import Barrier

import pytest
from psycopg import Connection

from app.import_data import DatasetImportError, ImportResult, import_dataset
from app.queries import compare_delivery, compare_delivery_stages

FIXTURES = Path(__file__).parent / "fixtures"
ORDERS = FIXTURES / "synthetic_orders.csv"
CUSTOMERS = FIXTURES / "synthetic_customers.csv"


def read_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.reader(file))


def write_rows(path: Path, rows: list[list[str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as file:
        csv.writer(file).writerows(rows)
    return path


def test_ac01_repeated_and_concurrent_imports(
    db: Connection, migrated_database_url: str
) -> None:
    ready = Barrier(4, timeout=10)

    def load() -> ImportResult:
        ready.wait()
        return import_dataset(
            migrated_database_url, ORDERS, CUSTOMERS, source="synthetic"
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = [future.result() for future in [pool.submit(load) for _ in range(4)]]
    first = results[0]
    assert first.order_count == 5
    assert all(result == first for result in results)
    assert (
        import_dataset(migrated_database_url, ORDERS, CUSTOMERS, source="synthetic")
        == first
    )
    assert db.execute(
        "SELECT count(*) FROM order_fact WHERE dataset_id = %s",
        (first.dataset_id,),
    ).fetchone() == (5,)
    assert db.execute(
        """
        SELECT orders_sha256, customers_sha256, covered_months, has_carrier_handoff
        FROM dataset_version WHERE dataset_id = %s
        """,
        (first.dataset_id,),
    ).fetchone() == (
        sha256(ORDERS.read_bytes()).hexdigest(),
        sha256(CUSTOMERS.read_bytes()).hexdigest(),
        [date(2018, 7, 1), date(2018, 8, 1)],
        True,
    )


def test_ac01_each_file_and_source_affect_version(
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    alternate_orders = tmp_path / ORDERS.name
    alternate_orders.write_bytes(ORDERS.read_bytes() + b"\n")
    alternate_customers = tmp_path / CUSTOMERS.name
    alternate_customers.write_bytes(CUSTOMERS.read_bytes() + b"\n")
    versions = {
        import_dataset(
            migrated_database_url, orders, customers, source=source
        ).dataset_id
        for orders, customers, source in [
            (ORDERS, CUSTOMERS, "synthetic"),
            (alternate_orders, CUSTOMERS, "synthetic"),
            (ORDERS, alternate_customers, "synthetic"),
            (ORDERS, CUSTOMERS, "test_source"),
        ]
    }
    assert len(versions) == 4


@pytest.mark.parametrize(
    ("file", "problem", "message"),
    [
        (
            "orders",
            "missing_header",
            r"orders.csv:1: missing required columns: order_id",
        ),
        (
            "customers",
            "missing_header",
            r"customers.csv:1: missing required columns: customer_id",
        ),
        ("orders", "duplicate", r"orders.csv:7: duplicate order_id.*line 2"),
        ("customers", "duplicate", r"customers.csv:4: duplicate customer_id.*line 2"),
        (
            "orders",
            "bad_purchase_time",
            r"orders.csv:6: invalid order_purchase_timestamp",
        ),
        (
            "orders",
            "blank_purchase_time",
            r"orders.csv:6: missing order_purchase_timestamp",
        ),
    ],
)
def test_ac01_failed_import_keeps_previous_version(
    db: Connection,
    migrated_database_url: str,
    tmp_path: Path,
    file: str,
    problem: str,
    message: str,
) -> None:
    first = import_dataset(migrated_database_url, ORDERS, CUSTOMERS, source="synthetic")
    before = db.execute("SELECT count(*) FROM dataset_version").fetchone()
    orders, customers = read_rows(ORDERS), read_rows(CUSTOMERS)
    rows = orders if file == "orders" else customers
    if problem == "missing_header":
        for row in rows:
            row.pop(0)
    elif problem == "duplicate":
        rows.append(rows[1].copy())
    else:
        rows[-1][3] = "invalid" if problem == "bad_purchase_time" else ""
    orders_path = write_rows(tmp_path / "orders.csv", orders)
    customers_path = write_rows(tmp_path / "customers.csv", customers)
    with pytest.raises(DatasetImportError, match=message):
        import_dataset(
            migrated_database_url, orders_path, customers_path, source="synthetic"
        )
    assert db.execute("SELECT count(*) FROM dataset_version").fetchone() == before
    result = compare_delivery(
        db,
        dataset_id=first.dataset_id,
        month="2018-08",
        metric="late_rate",
        authorized_state=None,
    )
    assert result.rows[0].current.numerator == 1
    assert result.rows[0].current.sample_count == 2


def test_ac01_missing_match_and_invalid_nullable_events(
    db: Connection,
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    customers = read_rows(CUSTOMERS)[:2]
    customers[1][1] = " sp "
    orders = read_rows(ORDERS)
    orders[5][4] = "bad estimate"
    orders[5][5] = "2018-08-02T09:00:00Z"
    orders[5][6] = "bad carrier date"
    imported = import_dataset(
        migrated_database_url,
        write_rows(tmp_path / "orders.csv", orders),
        write_rows(tmp_path / "customers.csv", customers),
        source="synthetic",
    )
    dataset_id = imported.dataset_id
    quality = db.execute(
        "SELECT quality_counts FROM dataset_version WHERE dataset_id = %s",
        (dataset_id,),
    ).fetchone()[0]
    assert quality["unmatched_customer_count"] == 1
    assert quality["invalid_estimated_delivery_date"] == 1
    assert quality["invalid_delivered_at"] == 1
    assert quality["invalid_handed_to_carrier_at"] == 1
    assert quality["missing_delivered_at"] == 1
    assert db.execute(
        """
        SELECT state_code, city_name, purchased_at, estimated_delivery_date, delivered_at,
               handed_to_carrier_at
        FROM order_fact WHERE dataset_id = %s AND order_id = 'E'
        """,
        (dataset_id,),
    ).fetchone() == ("SP", "sao paulo", datetime(2018, 8, 1, 9), None, None, None)
    kwargs = dict(dataset_id=dataset_id, month="2018-08", metric="delivered_count")
    total = compare_delivery(db, authorized_state=None, **kwargs).rows[0]
    known_only = compare_delivery(db, authorized_state="SP", **kwargs).rows[0]
    groups = compare_delivery(
        db, group_by="state", authorized_state=None, **kwargs
    ).rows
    assert (total.current.value, known_only.current.value) == (3, 2)
    assert {row.region: row.current.value for row in groups} == {"SP": 2, None: 1}


def test_ac01_absent_handoff_column_keeps_regular_metrics(
    db: Connection,
    migrated_database_url: str,
    tmp_path: Path,
) -> None:
    rows = read_rows(ORDERS)
    orders_path = write_rows(tmp_path / "orders.csv", [row[:-1] for row in rows])
    imported = import_dataset(
        migrated_database_url, orders_path, CUSTOMERS, source="synthetic"
    )
    assert imported.order_count == 5
    assert db.execute(
        "SELECT has_carrier_handoff FROM dataset_version WHERE dataset_id = %s",
        (imported.dataset_id,),
    ).fetchone() == (False,)
    kwargs = dict(
        dataset_id=imported.dataset_id, month="2018-08", authorized_state=None
    )
    late = compare_delivery(db, metric="late_rate", **kwargs).rows[0]
    assert (late.current.numerator, late.current.sample_count) == (1, 2)
    stages = compare_delivery_stages(db, **kwargs)
    assert (stages.supported, stages.current, stages.baseline) == (False, None, None)
