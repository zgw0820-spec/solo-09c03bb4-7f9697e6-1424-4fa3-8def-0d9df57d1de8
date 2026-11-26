-- Account collation migration pre-check: schema
-- MySQL 8.0. Runs only on first initialization of the mysql_data volume.

CREATE DATABASE IF NOT EXISTS account_audit
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE account_audit;

-- The login column is STILL on the old binary collation (utf8mb4_bin),
-- i.e. the state before the planned migration to utf8mb4_0900_ai_ci.
CREATE TABLE IF NOT EXISTS account (
  id              BIGINT       NOT NULL,
  tenant_id       BIGINT       NOT NULL DEFAULT 0,
  login_name      VARCHAR(32)  CHARACTER SET utf8mb4 COLLATE utf8mb4_bin    NOT NULL,
  region_code     VARCHAR(8)   CHARACTER SET utf8mb4 COLLATE utf8mb4_bin    NOT NULL DEFAULT '',
  recovery_email  VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL,
  status          TINYINT      NOT NULL DEFAULT 1,
  remark          VARCHAR(255) NULL,
  created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  updated_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
  PRIMARY KEY (id),

  -- Plain single-column unique index on login_name.
  UNIQUE KEY uk_login_name (login_name),

  -- Composite unique index where another key part (tenant_id) must stay equal.
  UNIQUE KEY uk_tenant_login (tenant_id, login_name),

  -- Character-prefix unique index: only the first 4 characters are indexed.
  UNIQUE KEY uk_login_prefix4 (login_name(4)),

  -- Composite unique index where the non-login column keeps its OWN rule
  -- (region_code is utf8mb4_bin, so 'US' <> 'us').
  UNIQUE KEY uk_login_region (login_name, region_code),

  -- Composite unique index with a nullable part: keys containing NULL
  -- are allowed duplicates by InnoDB and must be skipped by the pre-check.
  UNIQUE KEY uk_login_recovery (login_name, recovery_email),

  -- Unique index that does NOT contain login_name; pre-check must ignore it.
  UNIQUE KEY uk_recovery_email (recovery_email),

  -- Non-unique index; pre-check must ignore it.
  KEY idx_status (status)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_0900_ai_ci
  COMMENT='Account table; login_name pending collation migration';
