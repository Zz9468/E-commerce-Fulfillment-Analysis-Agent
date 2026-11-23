"""Apply forward-only migrations to a PostgreSQL database."""

import os
from pathlib import Path

import psycopg
from psycopg import IsolationLevel

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def migrate(conninfo: str) -> list[str]:
    applied = []
    with psycopg.connect(conninfo) as connection:
        connection.isolation_level = IsolationLevel.READ_COMMITTED
        connection.execute("SELECT pg_advisory_xact_lock(743908221403611485)")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migration (
                filename text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        existing = {
            name
            for (name,) in connection.execute("SELECT filename FROM schema_migration")
        }
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in existing:
                continue
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migration (filename) VALUES (%s)", (path.name,)
            )
            applied.append(path.name)
    return applied


def main() -> None:
    conninfo = os.getenv("DATABASE_URL")
    if not conninfo:
        raise SystemExit("Set DATABASE_URL before running migrations.")
    for name in migrate(conninfo):
        print(f"Applied {name}")


if __name__ == "__main__":
    main()
