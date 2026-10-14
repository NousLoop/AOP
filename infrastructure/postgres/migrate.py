#!/usr/bin/env python3
"""Apply PostgreSQL schema migrations with version tracking (Phase 18).

Source of truth: infrastructure/postgres/init/*.sql
Tracking table:  schema_migrations(version, checksum, applied_at)

Usage:
  python infrastructure/postgres/migrate.py
  python infrastructure/postgres/migrate.py --status
  DATABASE_URL=postgresql://aop:aop@127.0.0.1:5432/aop python infrastructure/postgres/migrate.py
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent
INIT_DIR = ROOT / "init"
DEFAULT_URL = "postgresql://aop:aop@127.0.0.1:5432/aop"

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     TEXT PRIMARY KEY,
  checksum    TEXT NOT NULL,
  applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_sql(script: str) -> list[str]:
    """Split init/*.sql into statements, keeping $$ ... $$ bodies intact."""
    parts: list[str] = []
    buf: list[str] = []
    in_dollar = False
    for line in script.splitlines():
        stripped = line.strip()
        if not in_dollar and (not stripped or stripped.startswith("--")):
            continue
        buf.append(line)
        idx = 0
        while True:
            found = stripped.find("$$", idx)
            if found < 0:
                break
            in_dollar = not in_dollar
            idx = found + 2
        if not in_dollar and stripped.endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
    rest = "\n".join(buf).strip()
    if rest:
        parts.append(rest)
    return parts


def exec_script(conn, script: str) -> None:
    for stmt in split_sql(script):
        conn.execute(stmt)


def list_migrations() -> list[tuple[str, Path, str]]:
    files = sorted(INIT_DIR.glob("*.sql"))
    out: list[tuple[str, Path, str]] = []
    for path in files:
        version = path.stem  # e.g. 001_init
        sql = path.read_text(encoding="utf-8")
        out.append((version, path, sql))
    return out


def applied(conn) -> dict[str, str]:
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {r[0]: r[1] for r in rows}


def migrate(database_url: str, *, dry_run: bool = False) -> int:
    migrations = list_migrations()
    if not migrations:
        print("no migration files in", INIT_DIR)
        return 1

    with psycopg.connect(database_url) as conn:
        exec_script(conn, BOOTSTRAP)
        conn.commit()
        done = applied(conn)
        pending = []
        for version, path, sql in migrations:
            if version in done:
                if done[version] != _checksum(sql):
                    print(f"WARNING: checksum drift for {version} (DB vs file)")
                print(f"  skip  {version}")
                continue
            pending.append((version, path, sql))

        if not pending:
            print("schema up to date")
            return 0

        for version, path, sql in pending:
            print(f"  apply {version} ({path.name})")
            if dry_run:
                continue
            with conn.transaction():
                exec_script(conn, sql)
                conn.execute(
                    """
                    INSERT INTO schema_migrations (version, checksum)
                    VALUES (%s, %s)
                    ON CONFLICT (version) DO NOTHING
                    """,
                    (version, _checksum(sql)),
                )
        if dry_run:
            print(f"dry-run: {len(pending)} pending")
        else:
            print(f"applied {len(pending)} migration(s)")
    return 0


def status(database_url: str) -> int:
    migrations = list_migrations()
    with psycopg.connect(database_url) as conn:
        exec_script(conn, BOOTSTRAP)
        conn.commit()
        done = applied(conn)
    print(f"{'VERSION':<24} {'STATE':<10} CHECKSUM")
    for version, _path, sql in migrations:
        state = "applied" if version in done else "pending"
        print(f"{version:<24} {state:<10} {_checksum(sql)[:12]}...")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AOP PostgreSQL migrator")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_URL))
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.status:
            return status(args.database_url)
        return migrate(args.database_url, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        print("migrate failed:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
