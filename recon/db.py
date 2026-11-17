"""Database connection helpers.

Uses DATABASE_URL if set (any PostgreSQL 13+). Otherwise falls back to an
embedded PostgreSQL managed by `pgserver`, with its data directory in ./.pgdata,
so the project runs on a machine with no Postgres installed.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
_server = None


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    global _server
    if _server is None:
        try:
            import pgserver  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise SystemExit(
                "DATABASE_URL is not set and pgserver is not installed. "
                "Either export DATABASE_URL=postgresql://... or pip install pgserver"
            ) from exc
        _server = pgserver.get_server(str(ROOT / ".pgdata"), cleanup_mode=None)
    return _server.get_uri()


def pg_bin_dir() -> Path | None:
    """Directory containing psql/pg_dump for the embedded server (if used)."""
    if os.environ.get("DATABASE_URL"):
        return None
    try:
        import pgserver._commands as c  # type: ignore

        return Path(c.POSTGRES_BIN_PATH)
    except Exception:  # pragma: no cover
        return None


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(database_url(), autocommit=autocommit)


def run_sql_file(conn: psycopg.Connection, path: Path) -> None:
    sql = Path(path).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
