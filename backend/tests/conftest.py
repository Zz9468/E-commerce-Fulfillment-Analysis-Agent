import os
import secrets
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from psycopg import Connection, sql
from psycopg.conninfo import make_conninfo

from app.database import QUERY_ROLES, provision_query_login
from app.migrate import migrate


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--olist-dir", help="Directory containing the two original Olist CSVs"
    )
    parser.addoption(
        "--live-model",
        action="store_true",
        help="Run the live model smoke test with MODEL_BASE_URL, MODEL_NAME and MODEL_API_KEY",
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
    assert migrate(test_database_url) == [
        "001_order_data.sql",
        "002_query_permissions.sql",
    ]
    assert migrate(test_database_url) == []
    return test_database_url


@pytest.fixture(scope="session")
def query_database_urls(migrated_database_url: str) -> Iterator[dict[str, str]]:
    logins = {}
    with psycopg.connect(migrated_database_url, autocommit=True) as admin:
        try:
            with admin.transaction():
                for account_id, role in QUERY_ROLES.items():
                    login = f"test_{account_id[-2:]}_{uuid4().hex}"
                    password = secrets.token_urlsafe(32)
                    provision_query_login(admin, role, login, password)
                    logins[login] = (
                        account_id,
                        make_conninfo(
                            migrated_database_url, user=login, password=password
                        ),
                    )
            yield dict(logins.values())
        finally:
            for login in logins:
                admin.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login))
                )


@pytest.fixture
def db(migrated_database_url: str) -> Iterator[Connection]:
    with psycopg.connect(migrated_database_url, autocommit=True) as connection:
        yield connection
