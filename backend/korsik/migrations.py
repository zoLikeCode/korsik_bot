"""Versioned baseline migration and restricted service identities, operator-only."""

from pathlib import Path
from sqlalchemy import text
from .db import engine
from .models import Base


def migrate():
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(76810101)"))
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations (revision VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL)"
            )
        )
        versions = set(
            connection.execute(text("SELECT revision FROM schema_migrations")).scalars()
        )
        if "0001_initial" not in versions:
            Base.metadata.create_all(connection)
            connection.execute(
                text("INSERT INTO schema_migrations(revision) VALUES ('0001_initial')")
            )


def provision_roles():
    if engine.dialect.name != "postgresql":
        return
    from psycopg import sql

    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        for name in ("api", "worker", "scheduler", "transport"):
            role = "korsik_" + name
            path = Path("/run/secrets/db_" + name)
            if not path.is_file():
                raise RuntimeError("Missing service database secret: " + name)
            password = path.read_text().strip()
            exists = raw.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
            ).fetchone()
            if not exists:
                raw.execute(
                    sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role))
                )
            raw.execute(
                sql.SQL(
                    "ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
            raw.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(engine.url.database), sql.Identifier(role)
                )
            )
            raw.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
            if name == "transport":
                raw.execute(
                    sql.SQL("GRANT SELECT ON transport_deliveries TO {}").format(
                        sql.Identifier(role)
                    )
                )
                raw.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE ON transport_inbox TO {}"
                    ).format(sql.Identifier(role))
                )
                raw.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON transport_cursors TO {}"
                    ).format(sql.Identifier(role))
                )
            else:
                raw.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(sql.Identifier(role))
                )
        raw.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        raw.commit()
