"""Validate and publish a version of an Olist-shaped orders and customers CSV pair."""

import csv
import io
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import IsolationLevel
from psycopg.types.json import Jsonb

_ORDER_COLUMNS = {
    "order_id",
    "customer_id",
    "order_status",
    "order_purchase_timestamp",
    "order_estimated_delivery_date",
    "order_delivered_customer_date",
}
_CUSTOMER_COLUMNS = {"customer_id", "customer_state", "customer_city"}
_HANDOFF_COLUMN = "order_delivered_carrier_date"


class DatasetImportError(ValueError):
    """The input files cannot be published as a dataset."""


@dataclass(frozen=True)
class ImportResult:
    dataset_id: UUID
    order_count: int


class _CsvInput:
    def __init__(self, path: Path, required: set[str]) -> None:
        self.name = path.name
        raw = path.read_bytes()
        # The parser and version identifier use the same bytes even if the file changes later.
        self.digest = sha256(raw).hexdigest()
        self.reader = csv.DictReader(
            io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline=""),
            strict=True,
        )
        try:
            header = self.reader.fieldnames or []
        except (csv.Error, UnicodeError) as error:
            raise DatasetImportError(f"{self.name}:1: invalid CSV: {error}") from error
        if len(set(header)) != len(header):
            raise DatasetImportError(f"{self.name}:1: duplicate header")
        self.columns = set(header)
        missing = required - self.columns
        if missing:
            raise DatasetImportError(
                f"{self.name}:1: missing required columns: {', '.join(sorted(missing))}"
            )

    def rows(self) -> Iterator[tuple[str, dict[str, str]]]:
        try:
            for row in self.reader:
                location = f"{self.name}:{self.reader.line_num}"
                if None in row or None in row.values():
                    raise DatasetImportError(
                        f"{location}: row length differs from header"
                    )
                yield location, row
        except (csv.Error, UnicodeError) as error:
            raise DatasetImportError(
                f"{self.name}:{self.reader.reader.line_num + 1}: invalid CSV: {error}"
            ) from error


def _unique_key(
    row: dict[str, str],
    column: str,
    location: str,
    seen: dict[str, str],
) -> str:
    key = row[column].strip()
    if not key:
        raise DatasetImportError(f"{location}: missing {column}")
    if key in seen:
        raise DatasetImportError(
            f"{location}: duplicate {column} (first seen at line {seen[key]})"
        )
    seen[key] = location.rsplit(":", 1)[-1]
    return key


def _customer_regions(customers: _CsvInput) -> dict[str, tuple[str | None, str | None]]:
    seen: dict[str, str] = {}
    regions = {}
    for location, row in customers.rows():
        key = _unique_key(row, "customer_id", location, seen)
        state = row["customer_state"].strip().upper()
        city = row["customer_city"].strip() or None
        regions[key] = (
            (state, city) if re.fullmatch(r"[A-Z]{2}", state) else (None, None)
        )
    return regions


def _event_time(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if len(value) <= 10 or timestamp.tzinfo is not None:
        raise ValueError("expected a timestamp without a time zone")
    return timestamp


def _delivery_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return _event_time(value).date()


def _optional_event[T](
    value: str,
    name: str,
    parse: Callable[[str], T],
    quality: dict[str, int],
) -> T | None:
    if not value:
        quality[f"missing_{name}"] += 1
        return None
    try:
        return parse(value)
    except ValueError:
        quality[f"invalid_{name}"] += 1
        return None


def import_dataset(
    conninfo: str,
    orders_csv: Path,
    customers_csv: Path,
    *,
    source: str,
) -> ImportResult:
    """Publish a complete version atomically or return the existing version."""
    source = source.strip()
    if not source:
        raise DatasetImportError("source is required")
    orders = _CsvInput(orders_csv, _ORDER_COLUMNS)
    customers = _CsvInput(customers_csv, _CUSTOMER_COLUMNS)
    has_handoff = _HANDOFF_COLUMN in orders.columns
    identity = (source, orders.digest, customers.digest)
    with psycopg.connect(conninfo) as connection:
        connection.isolation_level = IsolationLevel.READ_COMMITTED
        inserted = connection.execute(
            """
            INSERT INTO dataset_version (source, orders_sha256, customers_sha256, has_carrier_handoff)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source, orders_sha256, customers_sha256) DO NOTHING
            RETURNING dataset_id
            """,
            (*identity, has_handoff),
        ).fetchone()
        if inserted is None:
            # The unique index waits for a concurrent import. This statement sees its commit.
            existing = connection.execute(
                """
                SELECT dataset_id, order_count FROM dataset_version
                WHERE source = %s AND orders_sha256 = %s AND customers_sha256 = %s
                """,
                identity,
            ).fetchone()
            return ImportResult(*existing)

        (dataset_id,) = inserted
        regions = _customer_regions(customers)
        order_ids: dict[str, str] = {}
        covered_months: set[date] = set()
        quality = {
            "unmatched_customer_count": 0,
            **{
                f"{problem}_{event}": 0
                for event in (
                    "estimated_delivery_date",
                    "delivered_at",
                    "handed_to_carrier_at",
                )
                for problem in ("missing", "invalid")
            },
        }
        with connection.cursor().copy("""
            COPY order_fact (
                dataset_id, order_id, order_status, purchased_at, estimated_delivery_date,
                delivered_at, handed_to_carrier_at, state_code, city_name
            ) FROM STDIN
        """) as copy:
            for location, row in orders.rows():
                order_id = _unique_key(row, "order_id", location, order_ids)
                status = row["order_status"].strip()
                if not status:
                    raise DatasetImportError(f"{location}: missing order_status")
                purchase = row["order_purchase_timestamp"].strip()
                if not purchase:
                    raise DatasetImportError(
                        f"{location}: missing order_purchase_timestamp"
                    )
                try:
                    purchased_at = _event_time(purchase)
                except ValueError as error:
                    raise DatasetImportError(
                        f"{location}: invalid order_purchase_timestamp"
                    ) from error
                estimate = _optional_event(
                    row["order_estimated_delivery_date"].strip(),
                    "estimated_delivery_date",
                    _delivery_date,
                    quality,
                )
                delivered = _optional_event(
                    row["order_delivered_customer_date"].strip(),
                    "delivered_at",
                    _event_time,
                    quality,
                )
                handoff = None
                if has_handoff:
                    handoff = _optional_event(
                        row[_HANDOFF_COLUMN].strip(),
                        "handed_to_carrier_at",
                        _event_time,
                        quality,
                    )
                customer_id = row["customer_id"].strip()
                if customer_id not in regions:
                    quality["unmatched_customer_count"] += 1
                state, city = regions.get(customer_id, (None, None))
                covered_months.add(purchased_at.date().replace(day=1))
                copy.write_row(
                    (
                        dataset_id,
                        order_id,
                        status,
                        purchased_at,
                        estimate,
                        delivered,
                        handoff,
                        state,
                        city,
                    )
                )
        order_count = len(order_ids)
        connection.execute(
            """
            UPDATE dataset_version
            SET covered_months = %s, order_count = %s, quality_counts = %s
            WHERE dataset_id = %s
            """,
            (sorted(covered_months), order_count, Jsonb(quality), dataset_id),
        )
        return ImportResult(dataset_id, order_count)
