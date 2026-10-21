"""One-time migration of the existing ARQX store.db catalog into Supabase PostgreSQL.
Run locally with store.db in this folder and DATABASE_URL set in the environment.
"""
import os
import sqlite3
import sys

import psycopg
from psycopg import sql

DATABASE_URL = os.environ.get("DATABASE_URL", "")
SQLITE_PATH = os.environ.get("SQLITE_PATH", "store.db")
TABLES = (
    "files",
    "shares",
    "channels",
    "admins",
    "command_menu_users",
    "settings",
)


def main():
    if not DATABASE_URL:
        raise SystemExit("Set DATABASE_URL to the Supabase PostgreSQL connection string first.")
    if not os.path.isfile(SQLITE_PATH):
        raise SystemExit(f"SQLite database not found: {SQLITE_PATH}")

    source = sqlite3.connect(SQLITE_PATH)
    source.row_factory = sqlite3.Row
    total = 0
    try:
        with psycopg.connect(DATABASE_URL, sslmode="require", connect_timeout=10, prepare_threshold=None) as target:
            with target.cursor() as cursor:
                for table in TABLES:
                    exists = source.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                    if not exists:
                        print(f"SKIP {table}: not present in source database")
                        continue
                    columns = [r["name"] for r in source.execute(f'PRAGMA table_info("{table}")')]
                    rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
                    if not rows:
                        print(f"OK {table}: 0 rows")
                        continue
                    query = sql.SQL("INSERT INTO public.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
                        sql.Identifier(table),
                        sql.SQL(", ").join(map(sql.Identifier, columns)),
                        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                    )
                    cursor.executemany(query, [tuple(row[col] for col in columns) for row in rows])
                    total += len(rows)
                    print(f"OK {table}: processed {len(rows)} rows")
            target.commit()
    finally:
        source.close()
    print(f"Migration finished. Processed {total} source rows (existing primary keys were left unchanged).")


if __name__ == "__main__":
    main()
