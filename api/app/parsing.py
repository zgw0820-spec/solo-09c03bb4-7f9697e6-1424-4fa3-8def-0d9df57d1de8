"""Lenient parsing/validation of pre-check request bodies.

Supported JSON shapes (all equivalent)::

    {"changes": [{"id": 1, "login_name": "alice"}, ...]}
    {"changes": {"1": "alice", "2": "bob"}}
    [{"id": 1, "login_name": "alice"}, ...]
    {"1": "alice", "2": "bob"}
    {"ids": [1, 2], "login_names": ["alice", "bob"]}

Primary keys must be JSON integers (object keys are parsed as integers),
booleans are never accepted as ids.
"""

from __future__ import annotations

import re
from typing import Any

_INT_KEY_RE = re.compile(r"^[+-]?\d+$")

_ID_KEYS = ("id", "account_id", "accountid", "pk", "ID", "Id")
_LOGIN_KEYS = (
    "login_name",
    "loginName",
    "new_login_name",
    "newLoginName",
    "login",
    "username",
    "user_name",
)


class RequestParseError(ValueError):
    """Raised when the submitted payload cannot be normalized."""

    def __init__(self, message: str, *, details: Any | None = None):
        super().__init__(message)
        self.message = message
        self.details = details


def _strict_int_id(value: Any, *, where: str) -> int:
    # bool is a subclass of int - reject it explicitly.
    if isinstance(value, bool):
        raise RequestParseError(f"{where}: id must be an integer, got boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _INT_KEY_RE.match(value.strip()):
        return int(value.strip())
    raise RequestParseError(f"{where}: id must be an integer, got {value!r}")


def _login_value(value: Any, *, where: str) -> str:
    if not isinstance(value, str):
        raise RequestParseError(f"{where}: login_name must be a JSON string")
    return value


def _extract_pair(item: Any, *, where: str) -> tuple[int, str]:
    if not isinstance(item, dict):
        raise RequestParseError(f"{where}: expected an object with id/login_name")

    raw_id = None
    for key in _ID_KEYS:
        if key in item:
            raw_id = item[key]
            break
    if raw_id is None:
        raise RequestParseError(f"{where}: missing integer primary key ('id')")

    raw_login = None
    for key in _LOGIN_KEYS:
        if key in item:
            raw_login = item[key]
            break
    if raw_login is None:
        raise RequestParseError(f"{where}: missing 'login_name'")

    return _strict_int_id(raw_id, where=where), _login_value(raw_login, where=where)


def _mapping_to_pairs(mapping: dict[Any, Any], *, where: str) -> list[tuple[int, str]]:
    pairs: list[tuple[int, str]] = []
    for key, value in mapping.items():
        ident = _strict_int_id(key, where=f"{where} key {key!r}")
        if isinstance(value, str):
            login = value
        elif isinstance(value, dict):
            # e.g. {"1001": {"login_name": "alice"}}
            _, login = _extract_pair(value, where=f"{where}.{key}")
        else:
            raise RequestParseError(
                f"{where}.{key}: value must be a login-name string or an object"
            )
        pairs.append((ident, login))
    return pairs


def parse_changes(payload: Any) -> list[tuple[int, str]]:
    """Normalize any supported payload shape into a list of (id, new_login)."""
    if payload is None:
        return []

    # {"ids": [...], "login_names": [...]} parallel-array form
    if isinstance(payload, dict) and (
        "ids" in payload or "account_ids" in payload
    ) and ("login_names" in payload or "names" in payload or "logins" in payload):
        ids = payload.get("ids", payload.get("account_ids"))
        names = payload.get("login_names", payload.get("names", payload.get("logins")))
        if not isinstance(ids, list) or not isinstance(names, list):
            raise RequestParseError("'ids' and 'login_names' must be arrays")
        if len(ids) != len(names):
            raise RequestParseError("'ids' and 'login_names' length mismatch")
        pairs = []
        for i, (raw_id, raw_login) in enumerate(zip(ids, names)):
            pairs.append(
                (
                    _strict_int_id(raw_id, where=f"ids[{i}]"),
                    _login_value(raw_login, where=f"login_names[{i}]"),
                )
            )
        return _dedupe_check(pairs)

    if isinstance(payload, dict) and "changes" in payload:
        changes = payload["changes"]
        if changes is None:
            return []
        if isinstance(changes, list):
            pairs = [
                _extract_pair(item, where=f"changes[{i}]")
                for i, item in enumerate(changes)
            ]
            return _dedupe_check(pairs)
        if isinstance(changes, dict):
            return _dedupe_check(_mapping_to_pairs(changes, where="changes"))
        raise RequestParseError("'changes' must be an array or an id->login_name object")

    if isinstance(payload, list):
        pairs = [_extract_pair(item, where=f"[{i}]") for i, item in enumerate(payload)]
        return _dedupe_check(pairs)

    if isinstance(payload, dict):
        # Bare id -> login_name mapping.
        return _dedupe_check(_mapping_to_pairs(payload, where="payload"))

    raise RequestParseError("Request body must be a JSON object or array")


def _dedupe_check(pairs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    seen: set[int] = set()
    duplicates: list[int] = []
    for ident, _ in pairs:
        if ident in seen and ident not in duplicates:
            duplicates.append(ident)
        seen.add(ident)
    if duplicates:
        raise RequestParseError(
            "Duplicate primary key(s) in batch; each id may appear only once",
            details={"duplicate_ids": sorted(duplicates)},
        )
    return pairs
