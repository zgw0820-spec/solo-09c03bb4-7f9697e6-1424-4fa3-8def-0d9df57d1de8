"""Configuration from environment variables (12-factor style)."""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings:
    # MySQL connection
    db_host: str = os.getenv("DB_HOST", "127.0.0.1")
    db_port: int = _int("DB_PORT", 3306)
    db_name: str = os.getenv("DB_NAME", "account_audit")
    db_user: str = os.getenv("DB_USER", "audit")
    db_password: str = os.getenv("DB_PASSWORD", "auditpw")
    db_connect_timeout: int = _int("DB_CONNECT_TIMEOUT", 10)

    # Account table / columns (override if your schema uses other names)
    account_schema: str = os.getenv("ACCOUNT_SCHEMA", os.getenv("DB_NAME", "account_audit"))
    account_table: str = os.getenv("ACCOUNT_TABLE", "account")
    id_column: str = os.getenv("ID_COLUMN", "id")
    login_column: str = os.getenv("LOGIN_COLUMN", "login_name")

    # Collation the login column is migrating TO
    target_collation: str = os.getenv("TARGET_COLLATION", "utf8mb4_0900_ai_ci")


settings = Settings()
