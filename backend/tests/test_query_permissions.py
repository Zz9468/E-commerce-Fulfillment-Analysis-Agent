import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege

from scripts.seed_demo import seed_demo


def test_read_only_logins_enforce_scope_without_an_application_filter(
    migrated_database_url, query_database_urls
):
    dataset = seed_demo(migrated_database_url).dataset_id
    with psycopg.connect(migrated_database_url) as admin:
        unknown_dataset = admin.execute(
            "INSERT INTO dataset_version (source, orders_sha256, customers_sha256, has_carrier_handoff) "
            "VALUES ('permissions', repeat('0', 64), repeat('0', 64), false) RETURNING dataset_id"
        ).fetchone()[0]
        admin.execute(
            "INSERT INTO order_fact (dataset_id, order_id, order_status, purchased_at) "
            "VALUES (%s, 'unknown', 'delivered', '2018-08-01')",
            (unknown_dataset,),
        )
    groups = {}
    for account, url in query_database_urls.items():
        with psycopg.connect(url, autocommit=True) as connection:
            assert connection.execute("SHOW transaction_read_only").fetchone() == (
                "on",
            )
            assert connection.execute(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                "FROM pg_roles WHERE rolname = current_user"
            ).fetchone() == (False, False, False, False)
            assert connection.execute(
                "SELECT pg_get_userbyid(relowner) = current_user "
                "FROM pg_class WHERE oid = 'order_fact'::regclass"
            ).fetchone() == (False,)
            groups[account] = dict(
                connection.execute(
                    "SELECT state_code, count(*) FROM order_fact "
                    "WHERE dataset_id = %s GROUP BY state_code",
                    (dataset,),
                ).fetchall()
            )
            assert connection.execute(
                "SELECT COUNT(*) FROM order_fact WHERE dataset_id = %s",
                (unknown_dataset,),
            ).fetchone() == (int(account == "demo-hq"),)
            connection.execute("SET default_transaction_read_only = off")
            with pytest.raises(InsufficientPrivilege):
                connection.execute("DELETE FROM order_fact WHERE false")
            with pytest.raises(InsufficientPrivilege):
                connection.execute("SELECT order_id FROM order_fact LIMIT 1")
            if account == "demo-sp":
                with pytest.raises(InsufficientPrivilege):
                    connection.execute(
                        "SELECT order_count, covered_months FROM dataset_version"
                    )
                with pytest.raises(InsufficientPrivilege):
                    connection.execute("SET ROLE fulfillment_read_hq")
    assert sum(groups["demo-hq"].values()) == 96
    assert groups["demo-sp"] == {"SP": groups["demo-hq"]["SP"]}
