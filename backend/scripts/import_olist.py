"""Import Olist or a synthetic CSV pair into the configured PostgreSQL database."""

import argparse
import json
import os
from pathlib import Path

import psycopg

from app.import_data import DatasetImportError, import_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("orders_csv", type=Path)
    parser.add_argument("customers_csv", type=Path)
    parser.add_argument("--source", choices=("olist", "synthetic"), default="olist")
    args = parser.parse_args()
    conninfo = os.getenv("DATABASE_URL")
    if not conninfo:
        parser.exit(1, "Set DATABASE_URL before importing data.\n")
    try:
        result = import_dataset(
            conninfo, args.orders_csv, args.customers_csv, source=args.source
        )
    except (DatasetImportError, OSError, psycopg.Error) as error:
        parser.exit(1, f"Import failed: {error}\n")
    print(
        json.dumps(
            {"dataset_id": str(result.dataset_id), "order_count": result.order_count}
        )
    )


if __name__ == "__main__":
    main()
