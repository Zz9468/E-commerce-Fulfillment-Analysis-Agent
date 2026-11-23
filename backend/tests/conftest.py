import os
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.conninfo import make_conninfo

from app.migrate import migrate


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--olist-dir", help="Directory containing the two original Olist CSVs"
    )


@pytest.fixture(scope="session")
def test_database_url() -> Iterator[str]:
    admin_url = os.getenv("TEST_DATABASE_ADMIN_URL")
    if not admin_url:
        pytest.fail("Set TEST_DATABASE_ADMIN_URL to run the PostgreSQL tests.")
    name = f"fulfillment_test_{uuid4().hex}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
        try:
            yield make_conninfo(admin_url, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def migrated_database_url(test_database_url: str) -> str:
    assert "001_order_data.sql" in migrate(test_database_url)
    assert migrate(test_database_url) == []
    return test_database_url


@pytest.fixture
def db(migrated_database_url: str) -> Iterator[Connection]:
    with psycopg.connect(migrated_database_url, autocommit=True) as connection:
        yield connection
