"""Authenticated same-origin API and production frontend entry point."""

from __future__ import annotations

from importlib import import_module
import logging
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from .common import json_safe
from .config import load_config
from .db import create_db_engine, create_session_factory
from .identity import COOKIE_NAME, token_hash
from .models import AuditLog, Session

LOG = logging.getLogger("platform.api")


class BodyLimitMiddleware:
    """Bound chunked as well as Content-Length requests before JSON parsing."""

    def __init__(self, app, limit=2 * 1024 * 1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        parts, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                response = JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "request_too_large",
                            "message": "请求体超过 2MiB 限制。",
                            "details": {},
                            "request_id": str(uuid4()),
                        }
                    },
                )
                return await response(scope, receive, send)
            parts.append(chunk)
            if not message.get("more_body", False):
                break
        body, delivered = b"".join(parts), False

        async def limited_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, limited_receive, send)


def create_app(config=None, session_factory=None):
    config = config or load_config()
    app = FastAPI(
        title="内部数据库 AI 运维平台",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.config = config
    if session_factory is None:
        app.state.engine = create_db_engine(config)
        session_factory = create_session_factory(app.state.engine, config)
    app.state.session_factory = session_factory
    app.state.pending_modules = []
    host = urlparse(config.public_origin).hostname
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[host, "127.0.0.1", "localhost"])
    app.add_middleware(BodyLimitMiddleware)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = str(uuid4())
        size = request.headers.get("content-length", "0")
        if not size.isdigit() or int(size) > 2 * 1024 * 1024:
            return error_response(request, 413, "request_too_large", "请求体超过 2MiB 限制。")
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def audit_denial(request, code):
        if not request.url.path.startswith("/api/v1/"):
            return
        try:
            with session_factory.begin() as db:
                token = request.cookies.get(COOKIE_NAME)
                session = (
                    db.query(Session).filter(Session.token_hash == token_hash(token)).first()
                    if token
                    else None
                )
                db.add(
                    AuditLog(
                        actor_id=session.user_id if session else None,
                        action="http.denied",
                        target_type="route",
                        target_id=request.url.path[:100],
                        result="denied",
                        request_id=request.state.request_id,
                        details={"code": code, "method": request.method},
                    )
                )
        except Exception:
            LOG.error("Audit persistence unavailable request_id=%s", request.state.request_id)

    def error_response(request, status, code, message, details=None, headers=None):
        if status in (401, 403, 404, 409):
            audit_denial(request, code)
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "details": json_safe(details or {}),
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
            headers=headers,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            return error_response(
                request,
                exc.status_code,
                detail["code"],
                detail.get("message", "请求失败。"),
                detail.get("details"),
                exc.headers,
            )
        codes = {
            401: "authentication_required",
            403: "action_forbidden",
            404: "not_found",
            409: "state_conflict",
            422: "invalid_input",
            429: "rate_limited",
            503: "dependency_unavailable",
        }
        return error_response(
            request,
            exc.status_code,
            codes.get(exc.status_code, "http_error"),
            str(detail) if isinstance(detail, str) else "请求失败。",
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Never return pydantic input/context or invalid user values (credentials).
        locations = [
            {"field": ".".join(str(part) for part in item["loc"]), "type": item["type"]}
            for item in exc.errors()
        ]
        return error_response(
            request, 422, "invalid_input", "输入不符合接口要求。", {"fields": locations}
        )

    @app.exception_handler(IntegrityError)
    async def conflict(request, exc):
        return error_response(
            request, 409, "data_conflict", "记录已存在或被同时更新，请刷新后重试。"
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        LOG.error("Database failure %s request_id=%s", type(exc).__name__, request.state.request_id)
        return error_response(request, 503, "control_database_unavailable", "控制数据库暂不可用。")

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        LOG.error("Unhandled %s request_id=%s", type(exc).__name__, request.state.request_id)
        return error_response(
            request, 500, "internal_error", "操作未完成，请凭请求编号查看运行记录。"
        )

    @app.get("/api/v1/health/live")
    def live():
        return {"process": "alive", "version": "1.0.0"}

    @app.get("/api/v1/health/ready")
    def ready():
        try:
            with session_factory() as db:
                db.execute(text("SELECT 1"))
                version = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
            if version != "0007_demo":
                return JSONResponse(
                    status_code=503,
                    content={
                        "ready": False,
                        "control_database": "reachable",
                        "migration": "mismatch",
                    },
                )
            return {
                "ready": True,
                "control_database": "ready",
                "migration": version,
                "pending_feature_modules": app.state.pending_modules,
            }
        except SQLAlchemyError:
            return JSONResponse(
                status_code=503, content={"ready": False, "control_database": "unavailable"}
            )

    # During incremental construction missing modules remain explicitly reported.
    # A present module with an import error must fail startup, never be hidden.
    for name in (
        "auth",
        "admin",
        "inventory",
        "observability",
        "tasks",
        "inbox",
        "events",
        "knowledge",
        "investigations",
        "explain",
        "manifests",
        "monitoring",
        "intelligence",
        "demo",
        "showcase",
        "readonly",
    ):
        if not (Path(__file__).parent / "routes" / f"{name}.py").is_file():
            app.state.pending_modules.append(name)
            continue
        app.include_router(import_module(f"platform_app.routes.{name}").router, prefix="/api/v1")

    frontend = Path(__file__).resolve().parents[1] / "frontend" / "dist"

    @app.get("/{path:path}", include_in_schema=False)
    def frontend_route(path: str):
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "API route not found")
        target = (frontend / path).resolve()
        if not target.is_relative_to(frontend.resolve()):
            raise HTTPException(404, "Resource not found")
        if path and target.is_file():
            return FileResponse(target)
        if (frontend / "index.html").is_file():
            return FileResponse(frontend / "index.html")
        raise HTTPException(503, "Frontend build unavailable")

    return app
