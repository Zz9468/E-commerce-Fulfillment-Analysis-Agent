"""Publish a deterministic synthetic dataset with three investigation scenarios."""

import csv
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from app.import_data import ImportResult, import_dataset

_CUSTOMERS = (
    ("capital", "SP", "sao paulo"),
    ("interior", "SP", "campinas"),
    ("rio", "RJ", "rio de janeiro"),
)
_LATE_COUNTS = {7: (1, 0, 1), 8: (6, 1, 1), 9: (6, 1, 1), 10: (6, 4, 4)}


def seed_demo(connection_url: str) -> ImportResult:
    with TemporaryDirectory() as directory:
        orders = Path(directory) / "orders.csv"
        customers = Path(directory) / "customers.csv"
        with customers.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(("customer_id", "customer_state", "customer_city"))
            writer.writerows(_CUSTOMERS)
        with orders.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(
                (
                    "order_id",
                    "customer_id",
                    "order_status",
                    "order_purchase_timestamp",
                    "order_estimated_delivery_date",
                    "order_delivered_customer_date",
                    "order_delivered_carrier_date",
                )
            )
            for month, late_counts in _LATE_COUNTS.items():
                period = f"2018-{month:02d}"
                for (customer, state, _), late_count in zip(
                    _CUSTOMERS, late_counts, strict=True
                ):
                    for index in range(12 if state == "RJ" else 6):
                        delivery_day = 7 if index < late_count else 4
                        writer.writerow(
                            (
                                f"{month}-{customer}-{index}",
                                customer,
                                "delivered",
                                f"{period}-01 09:00:00",
                                f"{period}-05",
                                f"{period}-{delivery_day:02d} 09:00:00",
                                f"{period}-02 09:00:00",
                            )
                        )
        return import_dataset(connection_url, orders, customers, source="synthetic")


def main() -> None:
    result = seed_demo(os.environ["DATABASE_URL"])
    print(
        json.dumps(
            {"dataset_id": str(result.dataset_id), "order_count": result.order_count}
        )
    )


if __name__ == "__main__":
    main()
