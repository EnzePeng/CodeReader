"""Loopback-only browser session security for CodeReader's local API."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from hmac import compare_digest
from http.cookies import SimpleCookie
from typing import Any, Awaitable, Callable, Dict, FrozenSet, Iterable, List, Tuple

COOKIE_NAME = "codereader_session"


class SecurityConfigurationError(ValueError):
    pass


def _frozen_nonempty(values: Iterable[str], field: str) -> FrozenSet[str]:
    result = frozenset(v.strip().lower() for v in values if v and v.strip())
    if not result:
        raise SecurityConfigurationError(f"{field} 必须显式配置")
    return result


@dataclass(frozen=True)
class SecuritySettings:
    allowed_hosts: FrozenSet[str]
    allowed_origins: FrozenSet[str]
    session_token: str
    is_test: bool = False
    cookie_name: str = COOKIE_NAME
    require_origin_on_unsafe: bool = True

    def __post_init__(self) -> None:
        if len(self.session_token) < 32:
            raise SecurityConfigurationError("会话令牌至少需要 32 个字符")
        if not self.allowed_hosts or not self.allowed_origins:
            raise SecurityConfigurationError("Host 与 Origin 白名单不能为空")

    @classmethod
    def production(cls, app_host: str, app_port: int) -> "SecuritySettings":
        if app_host != "127.0.0.1":
            raise SecurityConfigurationError(
                "正式模式仅允许 app_host=127.0.0.1；不得监听局域网或所有网卡"
            )
        if not 1 <= int(app_port) <= 65535:
            raise SecurityConfigurationError("app_port 超出有效范围")
        authority = f"127.0.0.1:{int(app_port)}"
        return cls(
            allowed_hosts=frozenset({authority}),
            allowed_origins=frozenset({f"http://{authority}"}),
            session_token=secrets.token_urlsafe(32),
        )

    @classmethod
    def development(
        cls,
        *,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str],
        session_token: str | None = None,
    ) -> "SecuritySettings":
        return cls(
            allowed_hosts=_frozen_nonempty(allowed_hosts, "allowed_hosts"),
            allowed_origins=_frozen_nonempty(allowed_origins, "allowed_origins"),
            session_token=session_token or secrets.token_urlsafe(32),
        )

    @classmethod
    def testing(
        cls,
        *,
        allowed_hosts: Iterable[str],
        allowed_origins: Iterable[str],
        session_token: str,
    ) -> "SecuritySettings":
        return cls(
            allowed_hosts=_frozen_nonempty(allowed_hosts, "allowed_hosts"),
            allowed_origins=_frozen_nonempty(allowed_origins, "allowed_origins"),
            session_token=session_token,
            is_test=True,
        )

    @property
    def session_id(self) -> str:
        return hashlib.sha256(self.session_token.encode("utf-8")).hexdigest()[:24]

    @property
    def set_cookie_value(self) -> str:
        cookie = SimpleCookie()
        cookie[self.cookie_name] = self.session_token
        morsel = cookie[self.cookie_name]
        morsel["path"] = "/api"
        morsel["httponly"] = True
        morsel["samesite"] = "Strict"
        return morsel.OutputString()


HeaderList = List[Tuple[bytes, bytes]]
ASGIApp = Callable[[Dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]]


def _headers(scope: Dict[str, Any]) -> Dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _error_payload(code: str, message: str) -> bytes:
    return json.dumps(
        {"error": {"code": code, "message": message}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


async def _reject(send: Callable[..., Awaitable[Any]], status: int, code: str, message: str) -> None:
    body = _error_payload(code, message)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class SessionSecurityMiddleware:
    """ASGI middleware enforcing loopback Host, Origin, and session cookie.

    Non-API HTML/static responses receive the random HttpOnly session cookie.
    Thus a normal navigation bootstraps the browser before its first API call.
    Safe same-origin requests often omit ``Origin``; if present it is always
    checked.  Unsafe methods require an explicit allowed Origin.
    """

    UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, app: ASGIApp, settings: SecuritySettings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Dict[str, Any], receive: Callable[..., Awaitable[Any]],
                       send: Callable[..., Awaitable[Any]]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        headers = _headers(scope)
        host = headers.get("host", "").lower()
        if host not in self.settings.allowed_hosts:
            await _reject(send, 403, "invalid_host", "请求 Host 不在本地白名单")
            return

        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.settings.allowed_origins:
            await _reject(send, 403, "invalid_origin", "请求 Origin 不在本地白名单")
            return

        if path.startswith("/api"):
            method = str(scope.get("method", "GET")).upper()
            if (self.settings.require_origin_on_unsafe
                    and method in self.UNSAFE_METHODS and origin is None):
                await _reject(send, 403, "origin_required", "写操作必须携带可信 Origin")
                return
            cookies = SimpleCookie()
            try:
                cookies.load(headers.get("cookie", ""))
                supplied = cookies.get(self.settings.cookie_name)
                value = supplied.value if supplied else ""
            except Exception:
                value = ""
            if not value or not compare_digest(value, self.settings.session_token):
                await _reject(send, 401, "invalid_session", "缺少或无效的本地会话")
                return
            scope.setdefault("state", {})["session_id"] = self.settings.session_id
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                response_headers: HeaderList = list(message.get("headers", []))
                response_headers.append(
                    (b"set-cookie", self.settings.set_cookie_value.encode("latin-1"))
                )
                response_headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, receive, send_with_cookie)
