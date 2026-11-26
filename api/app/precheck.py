"""Core collation-migration pre-check logic.

Guarantees implemented here
---------------------------
* Every unique index containing ``login_name`` is discovered from
  ``information_schema.STATISTICS`` - including composite indexes and
  character-prefix parts (``SUB_PART``).
* Conflict grouping/counting/id aggregation is executed **inside MySQL**
  (derived table + GROUP BY ... HAVING COUNT(*) > 1 + JSON_ARRAYAGG).
* ``login_name`` is compared with the target collation; every other key
  part keeps its original column collation.
* Rows whose composite key contains any NULL part are filtered out before
  grouping (MySQL unique indexes treat NULL parts as distinct).
* All index checks run in ONE ``REPEATABLE-READ`` transaction started with
  ``WITH CONSISTENT SNAPSHOT``, so every index sees the same committed
  snapshot while other sessions keep writing to ``account``.
* Submitted changes live in a session TEMPORARY TABLE and overlay the
  snapshot via LEFT JOIN; only the *final* post-batch values are modeled.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import db
from .config import settings

# Config-provided identifiers are interpolated (never parameterizable in DDL
# / identifiers), so restrict them to a safe charset.
_CONFIG_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COLLATION_RE = re.compile(r"^[A-Za-z0-9_]+$")
_GENERATED_RE = re.compile(r"\b(?:VIRTUAL|STORED)\s+GENERATED\b", re.IGNORECASE)


class PrecheckConfigError(RuntimeError):
    """The service/schema is not usable for a pre-check (-> 503/500)."""


class UnknownAccountIds(LookupError):
    def __init__(self, unknown_ids: list[int]):
        super().__init__(f"Unknown account primary keys: {unknown_ids}")
        self.unknown_ids = unknown_ids


def assert_safe_configured_identifier(value: str, what: str) -> str:
    if not value or not _CONFIG_IDENT_RE.match(value):
        raise PrecheckConfigError(f"Configured {what} is not a safe identifier: {value!r}")
    return value


def quote_ident(name: Any) -> str:
    """Backtick-quote an identifier found in the catalog."""
    return "`" + str(name).replace("`", "``") + "`"


def _maybe_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


@dataclass
class KeyPart:
    seq: int
    column: str | None
    sub_part: int | None = None
    expression: str | None = None
    data_type: str | None = None
    column_collation: str | None = None
    extra: str | None = None

    @property
    def is_ordinary_column(self) -> bool:
        return self.column is not None and self.expression is None

    @property
    def is_generated(self) -> bool:
        return bool(self.extra and _GENERATED_RE.search(self.extra))

    def descriptor(self, *, login_column: str, target_collation: str) -> dict[str, Any]:
        is_login = (
            self.column is not None
            and self.column.lower() == login_column.lower()
        )
        if is_login:
            comparison_collation = target_collation
        else:
            # The bare column expression inherits its original collation.
            comparison_collation = self.column_collation
        return {
            "seq": self.seq,
            "column": self.column,
            "prefix_length": int(self.sub_part) if self.sub_part else None,
            "functional_expression": self.expression,
            "comparison_collation": comparison_collation,
            "target_collation_applied": is_login,
        }


@dataclass
class UniqueIndex:
    index_name: str
    non_unique: int
    parts: list[KeyPart] = field(default_factory=list)

    @property
    def is_primary(self) -> bool:
        return self.index_name == "PRIMARY"

    def contains_login(self, login_column: str) -> bool:
        return any(
            p.column is not None and p.column.lower() == login_column.lower()
            for p in self.parts
        )

    def references_login_expression(self, login_column: str) -> bool:
        """True for functional key parts such as ``lower(`login_name`)``."""
        bare = re.escape(login_column)
        pattern = re.compile(rf"(?:`{bare}`|\b{bare}\b)", re.IGNORECASE)
        return any(bool(p.expression and pattern.search(p.expression)) for p in self.parts)

    def unsupported_reason(self) -> str | None:
        for p in self.parts:
            if not p.is_ordinary_column:
                return (
                    "contains a functional/expression key part, which is not an "
                    "ordinary column index"
                )
            if p.is_generated:
                return (
                    f"key part {p.column!r} is a generated column; its value may "
                    "depend on login_name and cannot be overlaid safely"
                )
        return None


def discover_indexes(cur: Any, schema: str, table: str) -> list[UniqueIndex]:
    """Read index metadata from information_schema and group by index name."""
    cur.execute(
        """
        SELECT s.INDEX_NAME      AS index_name,
               s.NON_UNIQUE      AS non_unique,
               s.SEQ_IN_INDEX    AS seq_in_index,
               s.COLUMN_NAME     AS column_name,
               s.SUB_PART        AS sub_part,
               s.EXPRESSION      AS expression,
               c.DATA_TYPE       AS data_type,
               c.COLLATION_NAME  AS column_collation,
               c.EXTRA           AS extra
          FROM information_schema.STATISTICS s
          LEFT JOIN information_schema.COLUMNS c
            ON c.TABLE_SCHEMA  = s.TABLE_SCHEMA
           AND c.TABLE_NAME    = s.TABLE_NAME
           AND c.COLUMN_NAME   = s.COLUMN_NAME
         WHERE s.TABLE_SCHEMA = %s
           AND s.TABLE_NAME   = %s
         ORDER BY s.INDEX_NAME, s.SEQ_IN_INDEX
        """,
        (schema, table),
    )
    grouped: dict[str, UniqueIndex] = {}
    for row in cur.fetchall():
        name = row["index_name"]
        idx = grouped.get(name)
        if idx is None:
            idx = UniqueIndex(index_name=name, non_unique=int(row["non_unique"]))
            grouped[name] = idx
        idx.parts.append(
            KeyPart(
                seq=int(row["seq_in_index"]),
                column=row["column_name"],
                sub_part=int(row["sub_part"]) if row["sub_part"] is not None else None,
                expression=row["expression"],
                data_type=row["data_type"],
                column_collation=row["column_collation"],
                extra=row["extra"],
            )
        )
    return list(grouped.values())


def fetch_login_column_meta(
    cur: Any, schema: str, table: str, login_column: str
) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT COLUMN_NAME              AS column_name,
               DATA_TYPE                AS data_type,
               CHARACTER_MAXIMUM_LENGTH AS char_max_length,
               COLLATION_NAME           AS collation_name,
               IS_NULLABLE               AS is_nullable,
               COLUMN_TYPE              AS column_type
          FROM information_schema.COLUMNS
         WHERE TABLE_SCHEMA = %s
           AND TABLE_NAME   = %s
           AND COLUMN_NAME  = %s
        """,
        (schema, table, login_column),
    )
    return cur.fetchone()


def collation_exists(cur: Any, collation: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.COLLATIONS WHERE COLLATION_NAME = %s",
        (collation,),
    )
    return cur.fetchone() is not None


def _render_derived_table(
    *,
    schema_q: str,
    table_q: str,
    id_q: str,
    login_column: str,
    target_collation: str,
    temp_q: str,
    parts: list[KeyPart],
) -> tuple[str, list[str]]:
    """Build the candidate relation: snapshot account rows overlaid with changes.

    Returns the SQL for the derived table plus the list of key aliases.
    """
    select_items = [f"a.{id_q} AS `id`"]
    aliases: list[str] = []

    for i, part in enumerate(parts):
        alias = f"k{i}"
        aliases.append(alias)
        col_q = quote_ident(part.column)
        is_login = part.column is not None and part.column.lower() == login_column.lower()

        if is_login:
            # Force the TARGET collation on both sides before COALESCE so there
            # is no "Illegal mix of collations" and comparison uses new rule.
            expr = (
                "COALESCE("
                f"c.`new_login_name` COLLATE {target_collation}, "
                f"a.{col_q} COLLATE {target_collation}"
                ")"
            )
            if part.sub_part:
                # Prefix index: compare only the indexed character prefix.
                expr = f"LEFT({expr}, {int(part.sub_part)})"
            expr = f"{expr} COLLATE {target_collation}"
        else:
            # Other key parts deliberately keep their original collation.
            expr = f"a.{col_q}"
            if part.sub_part:
                expr = f"LEFT({expr}, {int(part.sub_part)})"

        select_items.append(f"{expr} AS `{alias}`")

    derived = (
        "SELECT " + ", ".join(select_items)
        + f" FROM {schema_q}.{table_q} a"
        + f" LEFT JOIN {temp_q} c ON c.`id` = a.{id_q}"
    )
    return derived, aliases


def build_index_queries(
    *,
    schema: str,
    table: str,
    id_column: str,
    login_column: str,
    target_collation: str,
    temp_table: str,
    index: UniqueIndex,
) -> tuple[str, str]:
    schema_q = quote_ident(schema)
    table_q = quote_ident(table)
    id_q = quote_ident(id_column)
    temp_q = quote_ident(temp_table)

    derived, aliases = _render_derived_table(
        schema_q=schema_q,
        table_q=table_q,
        id_q=id_q,
        login_column=login_column,
        target_collation=target_collation,
        temp_q=temp_q,
        parts=index.parts,
    )

    not_null = " AND ".join(f"z.`{a}` IS NOT NULL" for a in aliases)
    group_cols = ", ".join(f"z.`{a}`" for a in aliases)
    key_json_args = ", ".join(f"z.`{a}`" for a in aliases)

    groups_sql = f"""
        SELECT JSON_ARRAYAGG(z.`id`)  AS account_ids_json,
               COUNT(*)               AS group_size,
               JSON_ARRAY({key_json_args}) AS group_key_json
          FROM ({derived}) z
         WHERE {not_null}
         GROUP BY {group_cols}
        HAVING COUNT(*) > 1
    """

    eligible_sql = f"SELECT COUNT(*) AS eligible_rows FROM ({derived}) z WHERE {not_null}"
    return groups_sql, eligible_sql


def run_precheck(pairs: list[tuple[int, str]]) -> dict[str, Any]:
    """Execute the full pre-check. Raises UnknownAccountIds / PrecheckConfigError."""
    schema = assert_safe_configured_identifier(settings.account_schema, "ACCOUNT_SCHEMA")
    table = assert_safe_configured_identifier(settings.account_table, "ACCOUNT_TABLE")
    id_column = assert_safe_configured_identifier(settings.id_column, "ID_COLUMN")
    login_column = assert_safe_configured_identifier(settings.login_column, "LOGIN_COLUMN")
    if not _COLLATION_RE.match(settings.target_collation):
        raise PrecheckConfigError(
            f"TARGET_COLLATION is not a safe collation name: {settings.target_collation!r}"
        )
    target_collation = settings.target_collation

    changed_ids = {ident for ident, _ in pairs}
    conn = db.connect()
    temp_table = f"_pc_changes_{uuid.uuid4().hex}"

    try:
        with conn.cursor() as cur:
            if not collation_exists(cur, target_collation):
                raise PrecheckConfigError(
                    f"Target collation {target_collation!r} not found in this MySQL server"
                )

            login_meta = fetch_login_column_meta(cur, schema, table, login_column)
            if login_meta is None:
                raise PrecheckConfigError(
                    f"Login column {schema}.{table}.{login_column} not found"
                )
            char_max = login_meta.get("char_max_length")
            if char_max:
                too_long = [
                    {"id": ident, "length": len(login), "max_length": int(char_max)}
                    for ident, login in pairs
                    if len(login) > int(char_max)
                ]
                if too_long:
                    # Request-shape error surfaced as 422 by the API layer.
                    from .parsing import RequestParseError

                    raise RequestParseError(
                        "One or more proposed login names exceed the column length",
                        details={"too_long": too_long},
                    )

            # Session-local overlay of submitted final values. Created BEFORE
            # the consistent-snapshot transaction (TEMPORARY DDL does not
            # implicitly commit, but this ordering removes all ambiguity).
            cur.execute(
                f"CREATE TEMPORARY TABLE {quote_ident(temp_table)} ("
                "`id` BIGINT NOT NULL PRIMARY KEY, "
                f"`new_login_name` TEXT CHARACTER SET utf8mb4 "
                f"COLLATE {target_collation} NOT NULL"
                ") ENGINE=InnoDB"
            )
            if pairs:
                cur.executemany(
                    f"INSERT INTO {quote_ident(temp_table)} (`id`, `new_login_name`)"
                    " VALUES (%s, %s)",
                    pairs,
                )

            # One read view for every query below this line.
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")

            cur.execute("SELECT VERSION() AS version, @@transaction_isolation AS isolation")
            server_info = cur.fetchone()

            # Whole-batch rejection if ANY submitted primary key is unknown
            # in the same committed snapshot.
            cur.execute(
                f"SELECT c.`id` AS id FROM {quote_ident(temp_table)} c"
                f" LEFT JOIN {quote_ident(schema)}.{quote_ident(table)} a"
                f" ON a.{quote_ident(id_column)} = c.`id`"
                f" WHERE a.{quote_ident(id_column)} IS NULL"
                " ORDER BY c.`id`"
            )
            unknown_ids = [int(r["id"]) for r in cur.fetchall()]
            if unknown_ids:
                raise UnknownAccountIds(unknown_ids)

            cur.execute(
                f"SELECT COUNT(*) AS n FROM {quote_ident(schema)}.{quote_ident(table)}"
            )
            total_accounts = int(cur.fetchone()["n"])

            indexes = discover_indexes(cur, schema, table)
            login_unique_indexes = [
                idx
                for idx in indexes
                if idx.non_unique == 0
                and (
                    idx.contains_login(login_column)
                    or idx.references_login_expression(login_column)
                )
            ]

            checked: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            conflicts_by_index: dict[str, list[list[int]]] = {}
            total_groups = 0
            conflict_account_union: set[int] = set()

            for idx in sorted(login_unique_indexes, key=lambda x: x.index_name):
                reason = idx.unsupported_reason()
                if reason:
                    skipped.append(
                        {
                            "index_name": idx.index_name,
                            "primary": idx.is_primary,
                            "key_parts": [
                                p.descriptor(
                                    login_column=login_column,
                                    target_collation=target_collation,
                                )
                                for p in idx.parts
                            ],
                            "reason": reason,
                        }
                    )
                    continue

                groups_sql, eligible_sql = build_index_queries(
                    schema=schema,
                    table=table,
                    id_column=id_column,
                    login_column=login_column,
                    target_collation=target_collation,
                    temp_table=temp_table,
                    index=idx,
                )

                cur.execute(eligible_sql)
                eligible_rows = int(cur.fetchone()["eligible_rows"])

                cur.execute(groups_sql)
                rows = cur.fetchall()

                groups: list[dict[str, Any]] = []
                id_groups: list[list[int]] = []
                for row in rows:
                    ids = [int(i) for i in _maybe_json(row["account_ids_json"])]
                    ids.sort()
                    key_values = _maybe_json(row["group_key_json"])
                    group = {
                        "account_ids": ids,
                        "size": int(row["group_size"]),
                        "key_values": key_values,
                        "involves_submitted_change": any(i in changed_ids for i in ids),
                    }
                    groups.append(group)
                    id_groups.append(ids)
                    conflict_account_union.update(ids)

                # Deterministic ordering: groups by their smallest primary key.
                order = sorted(range(len(groups)), key=lambda g: groups[g]["account_ids"])
                groups = [groups[i] for i in order]
                id_groups = [id_groups[i] for i in order]

                null_skipped = total_accounts - eligible_rows
                checked.append(
                    {
                        "index_name": idx.index_name,
                        "primary": idx.is_primary,
                        "unique": True,
                        "key_parts": [
                            p.descriptor(
                                login_column=login_column,
                                target_collation=target_collation,
                            )
                            for p in idx.parts
                        ],
                        "rows_scanned": total_accounts,
                        "eligible_rows_after_null_filter": eligible_rows,
                        "null_key_rows_skipped": null_skipped,
                        "conflict_group_count": len(groups),
                        "conflict_account_count": sum(len(g["account_ids"]) for g in groups),
                        "conflict_groups": groups,
                    }
                )
                conflicts_by_index[idx.index_name] = id_groups
                total_groups += len(groups)

            cur.execute("ROLLBACK")

        result = {
            "status": "conflicts_found" if total_groups else "clean",
            "has_conflicts": total_groups > 0,
            "target_collation": target_collation,
            "account_table": f"{schema}.{table}",
            "id_column": id_column,
            "login_column": login_column,
            "total_accounts": total_accounts,
            "submitted_changes": len(pairs),
            "changed_ids": sorted(changed_ids),
            "unknown_ids": [],
            "snapshot": {
                "isolation_level": server_info.get("isolation"),
                "consistent_snapshot": True,
                "same_snapshot_for_all_indexes": True,
                "server_version": server_info.get("version"),
            },
            "indexes_checked_count": len(checked),
            "index_names_checked": [i["index_name"] for i in checked],
            "indexes": checked,
            "skipped_indexes": skipped,
            "conflicts_by_index": conflicts_by_index,
            # Convenience alias: index_name -> list of conflict groups (id arrays).
            "conflicts": conflicts_by_index,
            "total_conflict_groups": total_groups,
            "total_conflict_accounts": len(conflict_account_union),
        }
        return result
    except UnknownAccountIds:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK")
        raise
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def list_unique_indexes() -> dict[str, Any]:
    """Discovery-only view for GET /indexes."""
    schema = assert_safe_configured_identifier(settings.account_schema, "ACCOUNT_SCHEMA")
    table = assert_safe_configured_identifier(settings.account_table, "ACCOUNT_TABLE")
    login_column = assert_safe_configured_identifier(settings.login_column, "LOGIN_COLUMN")
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            indexes = discover_indexes(cur, schema, table)
            out = []
            for idx in sorted(indexes, key=lambda x: x.index_name):
                if idx.non_unique != 0:
                    continue
                out.append(
                    {
                        "index_name": idx.index_name,
                        "primary": idx.is_primary,
                        "contains_login_name": idx.contains_login(login_column),
                        "references_login_in_expression": idx.references_login_expression(
                            login_column
                        ),
                        "unsupported_reason": idx.unsupported_reason(),
                        "key_parts": [
                            p.descriptor(
                                login_column=login_column,
                                target_collation=settings.target_collation,
                            )
                            for p in idx.parts
                        ],
                    }
                )
            return {
                "account_table": f"{schema}.{table}",
                "login_column": login_column,
                "unique_indexes": out,
            }
    finally:
        conn.close()
