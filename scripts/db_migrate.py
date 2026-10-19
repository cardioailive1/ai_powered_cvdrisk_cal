"""Render preDeployCommand: bring the Postgres schema up to date with Prisma migrations.

    python scripts/db_migrate.py            # what render.yaml runs before every deploy

1. Database already managed by Prisma (has _prisma_migrations)  -> prisma migrate deploy
2. Empty database                                                -> prisma migrate deploy
3. Tables created by an earlier app version, no Prisma history   -> adopt them (prisma/adopt_existing_db.sql,
   keeps all rows), mark the init migration as applied, then prisma migrate deploy
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = "20261019000000_init"


def npx(*args: str) -> None:
    cmd = ["npx", "--no-install", "prisma", *args]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    import psycopg

    with psycopg.connect(url, autocommit=True) as con:
        tables = {r[0] for r in con.execute("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
        if "_prisma_migrations" not in tables and {"users", "assessments"} <= tables:
            print("Existing tables without Prisma history: adopting them into Prisma (rows are kept)", flush=True)
            with con.transaction():
                con.execute((ROOT / "prisma" / "adopt_existing_db.sql").read_text())
            adopted = True
        else:
            adopted = False
    if adopted:
        npx("migrate", "resolve", "--applied", INIT)
    npx("migrate", "deploy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
