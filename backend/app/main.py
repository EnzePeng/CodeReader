"""FastAPI application factory and local browser-session bootstrap."""
import asyncio
import atexit
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import api, cache, llama_launcher, llm
from .config import APP_VERSION, data_dir, get_config, static_dir
from .projects import ProjectError
from .schemas import APIError, APIErrorDetail
from .security import SecuritySettings, SessionSecurityMiddleware

logger = logging.getLogger("app.main")


def _error_response(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    body = APIError(error=APIErrorDetail(
        code=code, message=message, details=details)).model_dump(exclude_none=True)
    return JSONResponse(status_code=status_code, content=body,
                        headers={"Cache-Control": "no-store"})


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _configure_local_logging() -> None:
    logger = logging.getLogger("app")
    if any(getattr(handler, "_codereader", False) for handler in logger.handlers):
        return
    handler = RotatingFileHandler(
        data_dir() / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler._codereader = True  # type: ignore[attr-defined]
    handler.setFormatter(_JsonLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def create_app(*, security_settings: Optional[SecuritySettings] = None,
               start_model: bool = True) -> FastAPI:
    """Build the app with an explicit local security policy.

    Production may omit ``security_settings``; config must then bind to IPv4
    loopback. Tests/development construct their policy explicitly.
    """
    _configure_local_logging()
    if security_settings is None:
        cfg = get_config()
        security_settings = SecuritySettings.production(
            str(cfg["app_host"]), int(cfg["app_port"]))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if start_model:
            task = asyncio.create_task(llama_launcher.ensure_started())
        yield
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await llama_launcher.stop_async()
        await llm.close_client()
        cache.close()

    application = FastAPI(
        title="CodeReader", version=APP_VERSION, lifespan=lifespan)

    @application.exception_handler(ProjectError)
    async def project_error_handler(_: Request, exc: ProjectError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, str(exc))

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = {"issues": [
            {"path": ".".join(str(p) for p in issue.get("loc", ())),
             "message": issue.get("msg", "invalid value"),
             "type": issue.get("type", "validation_error")}
            for issue in exc.errors()
        ]}
        return _error_response(422, "validation_error", "请求参数无效", details)

    @application.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code", "http_error"))
            message = str(detail.get("message", "请求失败"))
            details = detail.get("details")
        else:
            code = "http_error"
            message = str(detail)
            details = None
        return _error_response(exc.status_code, code, message, details)

    @application.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", exc_info=exc)
        return _error_response(500, "internal_error", "服务内部错误")

    @application.exception_handler(404)
    async def not_found_handler(_: Request, __) -> JSONResponse:
        return _error_response(404, "not_found", "接口或资源不存在")

    application.include_router(api.router, prefix="/api")

    static = static_dir()
    if static.exists():
        application.mount("/", StaticFiles(directory=str(static), html=True), name="static")
    else:
        @application.get("/", include_in_schema=False)
        async def bootstrap() -> Response:
            return Response(
                "<!doctype html><title>CodeReader</title><p>Frontend is not built.</p>",
                media_type="text/html")

    application.add_middleware(
        SessionSecurityMiddleware, settings=security_settings)
    application.state.security_settings = security_settings
    return application


app = create_app()

atexit.register(llama_launcher.stop)
