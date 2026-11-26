"""FastAPI entrypoint for the collation migration pre-check."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import db
from .parsing import RequestParseError, parse_changes
from .precheck import (
    PrecheckConfigError,
    UnknownAccountIds,
    list_unique_indexes,
    run_precheck,
)

app = FastAPI(
    title="Account login-name collation pre-check",
    description=(
        "Pre-checks changing login_name collation from utf8mb4_bin to "
        "utf8mb4_0900_ai_ci against MySQL 8 unique indexes."
    ),
    version="1.0.0",
)


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "collation-precheck",
        "docs": "/docs",
        "endpoints": {
            "precheck": "POST /precheck",
            "indexes": "GET /indexes",
            "health": "GET /healthz",
        },
        "example": {
            "changes": [
                {"id": 1003, "login_name": "alice"},
                {"id": 1011, "login_name": "mallory"},
                {"id": 1012, "login_name": "MALLORY"},
            ]
        },
    }


@app.get("/healthz", tags=["meta"])
def healthz() -> JSONResponse:
    try:
        info = db.ping()
    except Exception as exc:  # pragma: no cover - depends on runtime
        return JSONResponse(
            status_code=503,
            content={"status": "db_unavailable", "error": str(exc)},
        )
    return JSONResponse(
        status_code=200,
        content={"status": "ok", "db": info},
    )


# Common aliases so the endpoint is easy to find behind gateways / test harnesses.
@app.get("/health", tags=["meta"], include_in_schema=False)
def health_alias() -> JSONResponse:
    return healthz()


@app.get("/indexes", tags=["precheck"])
@app.get("/api/indexes", tags=["precheck"], include_in_schema=False)
def indexes() -> JSONResponse:
    try:
        return JSONResponse(status_code=200, content=list_unique_indexes())
    except PrecheckConfigError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    except Exception as exc:
        return JSONResponse(status_code=503, content={"detail": f"database error: {exc}"})


async def _handle_precheck(request: Request) -> JSONResponse:
    raw = await _safe_json(request)
    if isinstance(raw, JSONResponse):
        return raw

    try:
        pairs = parse_changes(raw)
    except RequestParseError as exc:
        body: dict[str, Any] = {"detail": exc.message, "code": "INVALID_REQUEST"}
        if exc.details:
            body.update(exc.details)
        return JSONResponse(status_code=422, content=body)

    try:
        result = await run_in_threadpool(run_precheck, pairs)
    except UnknownAccountIds as exc:
        # Whole batch rejected: no index report is returned.
        return JSONResponse(
            status_code=404,
            content={
                "detail": "One or more submitted primary keys do not exist; entire batch rejected.",
                "code": "UNKNOWN_ACCOUNT_IDS",
                "unknown_ids": exc.unknown_ids,
                "submitted_count": len(pairs),
            },
        )
    except RequestParseError as exc:
        body = {"detail": exc.message, "code": "INVALID_REQUEST"}
        if exc.details:
            body.update(exc.details)
        return JSONResponse(status_code=422, content=body)
    except PrecheckConfigError as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "code": "PRECHECK_UNAVAILABLE"},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"detail": f"precheck failed: {type(exc).__name__}: {exc}"},
        )

    return JSONResponse(status_code=200, content=result)


async def _safe_json(request: Request) -> Any | JSONResponse:
    body = await request.body()  # Starlette caches this; request.json() can reuse it.
    if not body.strip():
        return {}
    try:
        import json

        return json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"detail": "Request body must be valid JSON", "code": "INVALID_JSON"},
        )


# Primary path plus likely aliases.
app.add_api_route("/precheck", _handle_precheck, methods=["POST"], tags=["precheck"])
app.add_api_route("/api/precheck", _handle_precheck, methods=["POST"], include_in_schema=False)
app.add_api_route(
    "/api/v1/collation-precheck",
    _handle_precheck,
    methods=["POST"],
    tags=["precheck"],
)
app.add_api_route(
    "/accounts/collation-precheck",
    _handle_precheck,
    methods=["POST"],
    include_in_schema=False,
)
