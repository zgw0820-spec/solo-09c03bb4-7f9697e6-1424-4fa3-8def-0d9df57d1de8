"""MySQL connection helpers.

Every pre-check request gets its own short-lived connection so that its
REPEATABLE-READ consistent snapshot and session TEMPORARY TABLE never leak
across requests.
"""

from __future__ import annotations

import time
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from .config import settings


def connect(*, autocommit: bool = True) -> pymysql.connections.Connection:
    """Open a new connection to MySQL 8 with utf8mb4."""
    return pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        autocommit=autocommit,
        connect_timeout=settings.db_connect_timeout,
        read_timeout=120,
        write_timeout=120,
        cursorclass=DictCursor,
    )


def ping() -> dict[str, Any]:
    """Return server version / current database for health checks."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS version, DATABASE() AS database_name")
            return cur.fetchone()
    finally:
        conn.close()


def wait_for_db(max_attempts: int = 30, delay_seconds: float = 2.0) -> bool:
    """Block until MySQL accepts connections. Used by local/scripted starts."""
    for attempt in range(1, max_attempts + 1):
        try:
            info = ping()
            if info and info.get("version"):
                return True
        except Exception:
            if attempt == max_attempts:
                return False
            time.sleep(delay_seconds)
    return False
