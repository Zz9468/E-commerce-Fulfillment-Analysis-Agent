"""Migrate, provision the two local query logins and publish the demo dataset."""

import os

import psycopg

from app.database import QUERY_ROLES, provision_query_login
from app.migrate import migrate
from scripts.seed_demo import seed_demo


def main() -> None:
    conninfo = os.getenv("DATABASE_URL", "")
    if not conninfo and not os.getenv("PGHOST"):
        raise SystemExit("Set DATABASE_URL or the PostgreSQL connection environment.")
    passwords = {
        role: os.environ.get(role.password_env) for role in QUERY_ROLES.values()
    }
    for role, password in passwords.items():
        if not password:
            raise SystemExit(f"Set {role.password_env} before bootstrapping the demo.")

    for name in migrate(conninfo):
        print(f"Applied {name}")
    with psycopg.connect(conninfo) as connection:
        for role, password in passwords.items():
            provision_query_login(connection, role, role.login, password)

    result = seed_demo(conninfo)
    print(f"Synthetic dataset {result.dataset_id} ({result.order_count} orders)")


if __name__ == "__main__":
    main()
