"""Key-flow self-tests for the collation pre-check.

Runs over HTTP against the compose-published API (default http://127.0.0.1:8000)
and, when DB_* are reachable, verifies the REPEATABLE-READ snapshot guarantee
directly in MySQL 8.

Set API_BASE_URL to override the HTTP target.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import pytest

# Skip this module (not collection-error) if the HTTP client dependency is absent.
httpx = pytest.importorskip("httpx")

try:
    import pymysql

    _pymysql_available = True
except ImportError:  # pragma: no cover
    pymysql = None  # type: ignore[assignment]
    _pymysql_available = False

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
# docker-compose publishes MySQL on host port 13306 (to avoid clashing with a
# host MySQL on 3306). Inside the api container DB_PORT=3306 is already set.
DB_PORT = int(os.getenv("DB_PORT", "13306"))
DB_USER = os.getenv("DB_USER", "audit")
DB_PASSWORD = os.getenv("DB_PASSWORD", "auditpw")
DB_NAME = os.getenv("DB_NAME", "account_audit")

BASELINE_LOGIN_GROUPS = [[1001, 1002], [1003, 1004], [1005, 1006], [1007, 1008]]


def _api_alive() -> bool:
    try:
        r = httpx.get(f"{API_BASE_URL}/healthz", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _api_alive(), reason="precheck API is not running")


def post_precheck(payload: Any) -> httpx.Response:
    return httpx.post(f"{API_BASE_URL}/precheck", json=payload, timeout=30)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_health_reports_mysql8() -> None:
    r = httpx.get(f"{API_BASE_URL}/healthz", timeout=5)
    assert r.status_code == 200
    assert r.json()["db"]["version"].startswith("8.")


def test_discovers_all_login_unique_indexes_including_prefix_and_composites() -> None:
    r = httpx.get(f"{API_BASE_URL}/indexes", timeout=10)
    assert r.status_code == 200
    indexes = {i["index_name"]: i for i in r.json()["unique_indexes"]}

    for name in [
        "uk_login_name",
        "uk_tenant_login",
        "uk_login_prefix4",
        "uk_login_region",
        "uk_login_recovery",
    ]:
        assert name in indexes, f"missing unique index {name}"

    # Indexes without login_name must not be considered login indexes.
    assert indexes["uk_recovery_email"]["contains_login_name"] is False
    assert indexes["PRIMARY"]["contains_login_name"] is False

    prefix = indexes["uk_login_prefix4"]
    login_part = next(p for p in prefix["key_parts"] if p["column"] == "login_name")
    assert login_part["prefix_length"] == 4

    tenant = indexes["uk_tenant_login"]
    assert [p["column"] for p in tenant["key_parts"]] == ["tenant_id", "login_name"]


# --------------------------------------------------------------------------- #
# Baseline conflict adjudication
# --------------------------------------------------------------------------- #


def test_baseline_conflict_groups_match_collation_semantics() -> None:
    r = post_precheck({})
    assert r.status_code == 200
    d = r.json()

    assert d["target_collation"] == "utf8mb4_0900_ai_ci"
    assert d["snapshot"]["consistent_snapshot"] is True
    assert d["snapshot"]["isolation_level"] == "REPEATABLE-READ"

    cbi = d["conflicts_by_index"]
    assert cbi["uk_login_name"] == BASELINE_LOGIN_GROUPS

    # Prefix-only collision abcd1000 vs ABCD2000 is caught by the prefix index.
    assert [1009, 1010] in cbi["uk_login_prefix4"]
    assert cbi["uk_login_prefix4"] == [
        [1001, 1002],
        [1003, 1004],
        [1005, 1006],
        [1007, 1008],
        [1009, 1010],
    ]

    # Different tenants keep (tenant_id, login) unique.
    assert cbi["uk_tenant_login"] == [[1001, 1002], [1007, 1008]]

    # region_code is utf8mb4_bin: 'us' vs 'US' must NOT collide.
    assert cbi["uk_login_region"] == [[1001, 1002], [1005, 1006], [1007, 1008]]

    # All four NULL-recovery rows are skipped -> no composite groups.
    recovery = next(i for i in d["indexes"] if i["index_name"] == "uk_login_recovery")
    assert recovery["null_key_rows_skipped"] == 4
    assert recovery["conflict_groups"] == []


# --------------------------------------------------------------------------- #
# Batch overlay semantics
# --------------------------------------------------------------------------- #


def test_trial_batch_merges_new_and_batch_internal_collisions() -> None:
    r = post_precheck(
        {
            "changes": [
                {"id": 1003, "login_name": "alice"},
                {"id": 1011, "login_name": "mallory"},
                {"id": 1012, "login_name": "MALLORY"},
            ]
        }
    )
    assert r.status_code == 200
    cbi = r.json()["conflicts_by_index"]

    # 1003 joins the alice/ALICE pair; its old 'bob' collision disappears.
    assert [1001, 1002, 1003] in cbi["uk_login_name"]
    assert [1011, 1012] in cbi["uk_login_name"]
    assert [1003, 1004] not in cbi["uk_login_name"]
    # Same on the composite/tenant indexes where other parts agree.
    assert [1001, 1002, 1003] in cbi["uk_tenant_login"]
    assert [1011, 1012] in cbi["uk_tenant_login"]


def test_swap_is_evaluated_only_on_final_batch_values() -> None:
    r = post_precheck(
        {
            "changes": [
                {"id": 1011, "login_name": "erin"},
                {"id": 1012, "login_name": "dave"},
            ]
        }
    )
    assert r.status_code == 200
    # A sequential "write each change" implementation would hit a duplicate
    # mid-batch; final-state overlay must show no 1011/1012 group.
    assert r.json()["conflicts_by_index"]["uk_login_name"] == BASELINE_LOGIN_GROUPS


def test_mapping_form_is_supported() -> None:
    r = post_precheck({"changes": {"1011": "zz_unique_1", "1012": "zz_unique_2"}})
    assert r.status_code == 200
    assert r.json()["changed_ids"] == [1011, 1012]


# --------------------------------------------------------------------------- #
# Whole-batch rejection / validation
# --------------------------------------------------------------------------- #


def test_unknown_primary_key_rejects_entire_batch() -> None:
    r = post_precheck(
        {"changes": [{"id": 1001, "login_name": "x"}, {"id": 999999, "login_name": "y"}]}
    )
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == "UNKNOWN_ACCOUNT_IDS"
    assert body["unknown_ids"] == [999999]
    # A rejected batch must not carry an index report.
    assert "indexes" not in body


def test_duplicate_id_is_422() -> None:
    r = post_precheck(
        {"changes": [{"id": 1, "login_name": "a"}, {"id": 1, "login_name": "b"}]}
    )
    assert r.status_code == 422


def test_boolean_id_is_422() -> None:
    r = post_precheck({"changes": [{"id": True, "login_name": "a"}]})
    assert r.status_code == 422


def test_overlong_login_is_422() -> None:
    r = post_precheck({"changes": [{"id": 1001, "login_name": "x" * 33}]})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


def test_invalid_json_is_400() -> None:
    r = httpx.post(
        f"{API_BASE_URL}/precheck",
        content="{not json",
        headers={"content-type": "application/json"},
        timeout=5,
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Concurrency: writers keep writing while prechecks run
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _pymysql_available, reason="PyMySQL not installed")
def test_prechecks_succeed_under_concurrent_writes() -> None:
    try:
        probe = pymysql.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
            database=DB_NAME, connect_timeout=3,
        )
        probe.close()
    except Exception as exc:
        pytest.skip(f"direct MySQL connection unavailable: {exc}")

    stop = threading.Event()
    errors: list[Exception] = []

    def writer() -> None:
        try:
            conn = pymysql.connect(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASSWORD,
                database=DB_NAME,
                charset="utf8mb4",
                autocommit=True,
            )
            i = 0
            with conn.cursor() as cur:
                while not stop.is_set():
                    i += 1
                    # Non-indexed column write: must never block/fail.
                    cur.execute(
                        "UPDATE account SET remark=%s WHERE id=1011",
                        (f"smoke-{i}",),
                    )
            with conn.cursor() as cur:
                cur.execute("UPDATE account SET remark='seed' WHERE id=1011")
            conn.close()
        except Exception as exc:  # pragma: no cover - failure reporting
            errors.append(exc)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        ok = 0
        for _ in range(8):
            r = post_precheck({})
            assert r.status_code == 200, r.text
            # remark writes cannot change login unique-index groups.
            assert r.json()["conflicts_by_index"]["uk_login_name"] == BASELINE_LOGIN_GROUPS
            ok += 1
            time.sleep(0.15)
        assert ok == 8
    finally:
        stop.set()
        t.join(timeout=10)
    assert not errors, errors


# --------------------------------------------------------------------------- #
# Direct MySQL snapshot guarantee (the mechanism behind "same snapshot")
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _pymysql_available, reason="PyMySQL not installed")
def test_mysql_consistent_snapshot_ignores_committed_writes_mid_check() -> None:
    try:
        admin = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=3,
        )
        snap = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=3,
        )
    except Exception as exc:
        pytest.skip(f"direct MySQL connection unavailable: {exc}")

    probe_id = 424242
    try:
        with admin.cursor() as cur:
            cur.execute("DELETE FROM account WHERE id=%s", (probe_id,))

        with snap.cursor() as cur:
            cur.execute("SET SESSION transaction_isolation='REPEATABLE-READ'")
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            cur.execute("SELECT COUNT(*) AS n FROM account")
            before = cur.fetchone()
            n_before = before["n"] if isinstance(before, dict) else before[0]

        # Another session commits a brand-new row while the snapshot tx is open.
        with admin.cursor() as cur:
            cur.execute(
                "INSERT INTO account (id, login_name) VALUES (%s, 'snapshot_probe')",
                (probe_id,),
            )

        with snap.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM account")
            row = cur.fetchone()
            n_during = row["n"] if isinstance(row, dict) else row[0]
            cur.execute("ROLLBACK")

        assert n_during == n_before, "consistent snapshot must not see new commits"

        # A fresh transaction does see the committed write.
        with snap.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM account")
            row = cur.fetchone()
            n_after = row["n"] if isinstance(row, dict) else row[0]
        assert n_after == n_before + 1
    finally:
        with admin.cursor() as cur:
            cur.execute("DELETE FROM account WHERE id=%s", (probe_id,))
        admin.close()
        snap.close()
