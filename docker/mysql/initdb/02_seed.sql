-- Seed data for trying the collation pre-check.
-- Every row satisfies ALL current unique indexes under utf8mb4_bin rules.
SET NAMES utf8mb4;
USE account_audit;

INSERT INTO account
  (id, tenant_id, login_name, region_code, recovery_email, status, remark)
VALUES
  -- 1001/1002: same login under 0900_ai_ci (case), same tenant+region.
  -- 1002 has NULL recovery_email -> skipped for uk_login_recovery.
  (1001, 1, 'alice',    'US', 'alice@example.com',    1, 'seed'),
  (1002, 1, 'ALICE',    'US', NULL,                   1, 'seed'),

  -- 1003/1004: case collision, but different tenant and bin-distinct region
  -- ('us' vs 'US'), so composite indexes stay clean.
  (1003, 1, 'bob',      'us', 'bob@example.com',      1, 'seed'),
  (1004, 2, 'BOB',      'US', NULL,                   1, 'seed'),

  -- 1005/1006: case collision, different tenant, SAME region.
  (1005, 2, 'carol',    'US', 'carol@example.com',    1, 'seed'),
  (1006, 3, 'Carol',    'US', NULL,                   1, 'seed'),

  -- 1007/1008: accent collision (e vs e-acute), same tenant+region.
  (1007, 4, 'renee',    'FR', 'renee@example.com',    1, 'seed'),
  (1008, 4, 'renée',    'FR', NULL,                   1, 'seed'),

  -- 1009/1010: first-4 chars collide under 0900_ai_ci ('abcd'='ABCD'),
  -- but full logins differ -> ONLY the prefix index reports them.
  (1009, 5, 'abcd1000', 'US', 'abcd1000@example.com', 1, 'seed'),
  (1010, 5, 'ABCD2000', 'US', 'abcd2000@example.com', 1, 'seed'),

  -- Clean singletons (used by the batch/swap self-tests).
  (1011, 6, 'dave',     'DE', 'dave@example.com',     1, 'seed'),
  (1012, 6, 'erin',     'DE', 'erin@example.com',     1, 'seed');
