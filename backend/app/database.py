import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import Connection, OperationalError, sql
from psycopg.conninfo import make_conninfo


@dataclass(frozen=True)
class QueryRole:
    group: str
    login: str
    password_env: str


QUERY_ROLES = {
    "demo-hq": QueryRole(
        "fulfillment_read_hq", "fulfillment_query_hq", "QUERY_HQ_PASSWORD"
    ),
    "demo-sp": QueryRole(
        "fulfillment_read_sp", "fulfillment_query_sp", "QUERY_SP_PASSWORD"
    ),
}


def configured_query_urls() -> dict[str, str]:
    return {
        account_id: make_conninfo(
            host=os.getenv("QUERY_DB_HOST", "127.0.0.1"),
            port=os.getenv("QUERY_DB_PORT", os.getenv("POSTGRES_PORT", "55432")),
            dbname=os.getenv("QUERY_DB_NAME", os.getenv("POSTGRES_DB", "fulfillment")),
            user=role.login,
            password=password,
        )
        for account_id, role in QUERY_ROLES.items()
        if (password := os.getenv(role.password_env))
    }


@contextmanager
def connect_for_query(
    conninfo: str | None,
    account_id: str,
    *,
    connect_timeout: int,
    statement_timeout: int,
) -> Iterator[Connection]:
    if not conninfo:
        raise OperationalError("Query database is not configured for this account")
    with psycopg.connect(
        conninfo,
        autocommit=True,
        connect_timeout=connect_timeout,
        options=(
            f"-c statement_timeout={statement_timeout} "
            "-c default_transaction_read_only=on -c search_path=pg_catalog,public"
        ),
    ) as connection:
        allowed = connection.execute(
            """
            SELECT pg_has_role(session_user, %s, 'USAGE')
                AND NOT pg_has_role(session_user, %s, 'USAGE')
                AND current_user = session_user
                AND row_security_active('public.order_fact')
            """,
            (
                QUERY_ROLES[account_id].group,
                next(
                    role.group for key, role in QUERY_ROLES.items() if key != account_id
                ),
            ),
        ).fetchone()
        if allowed != (True,):
            raise OperationalError("Query login does not match the authorized scope")
        yield connection


def provision_query_login(
    connection: Connection, role: QueryRole, login: str, password: str
) -> None:
    name = sql.Identifier(login)
    if (
        connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (login,)
        ).fetchone()
        is None
    ):
        connection.execute(sql.SQL("CREATE ROLE {}").format(name))
    verifier = connection.pgconn.encrypt_password(
        password.encode(), login.encode(), b"scram-sha-256"
    ).decode()
    connection.execute(
        sql.SQL(
            "ALTER ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB NOREPLICATION PASSWORD {}"
        ).format(name, sql.Literal(verifier))
    )
    connection.execute(
        sql.SQL("GRANT {} TO {} WITH INHERIT TRUE, SET FALSE, ADMIN FALSE").format(
            sql.Identifier(role.group), name
        )
    )
    connection.execute(
        sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(name)
    )
