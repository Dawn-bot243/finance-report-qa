"""API 鉴权与限流中间件。

环境变量：
    API_KEY                  设置后启用鉴权（X-API-Key 头或 Authorization: Bearer）
    RATE_LIMIT_PER_MINUTE    每 IP 每分钟请求上限（0/未设置 = 关闭应用层限流）

说明：生产部署时 nginx 层还有一层 limit_req（见 deploy/nginx.conf），
应用层限流用于未经过反代或反代被绕过的场景。
"""
from __future__ import annotations

import os
import threading
import time

from fastapi import Request
from fastapi.responses import JSONResponse

PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc", "/docs/oauth2-redirect"}


class _FixedWindowLimiter:
    """固定窗口限流：每 key 在 window 秒内最多 limit 次。"""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit = limit
        self.window = window
        self._state: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """返回 (是否放行, 剩余次数)。"""
        now = time.monotonic()
        with self._lock:
            start, count = self._state.get(key, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            if count >= self.limit:
                retry = int(self.window - (now - start)) + 1
                return False, retry
            self._state[key] = (start, count + 1)
            return True, self.limit - count - 1

    def cleanup(self) -> None:
        """清理过期窗口（避免内存无限增长）。"""
        now = time.monotonic()
        with self._lock:
            for k in [k for k, (s, _) in self._state.items() if now - s >= self.window]:
                self._state.pop(k, None)


_limiter_cache: dict[int, _FixedWindowLimiter] = {}


def _limiter() -> _FixedWindowLimiter | None:
    try:
        limit = int(os.getenv("RATE_LIMIT_PER_MINUTE", "0") or 0)
    except ValueError:
        limit = 0
    if limit <= 0:
        return None
    if limit not in _limiter_cache:
        _limiter_cache[limit] = _FixedWindowLimiter(limit)
    return _limiter_cache[limit]


def _client_key(request: Request) -> str:
    # 反代后取真实 IP（nginx 会设置 X-Real-IP）
    return (request.headers.get("X-Real-IP")
            or (request.client.host if request.client else "unknown"))


async def security_middleware(request: Request, call_next):
    """鉴权 + 限流。顺序：限流 → 鉴权（避免用错 key 刷接口）。"""
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)

    # 限流
    limiter = _limiter()
    if limiter is not None:
        allowed, extra = limiter.allow(_client_key(request))
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "请求过于频繁，请稍后重试"},
                headers={"Retry-After": str(extra)},
            )

    # 鉴权
    api_key = os.getenv("API_KEY", "")
    if api_key:
        provided = (request.headers.get("X-API-Key")
                    or request.headers.get("Authorization", "").removeprefix("Bearer ").strip())
        if provided != api_key:
            return JSONResponse(status_code=401,
                                content={"detail": "无效或缺失的 API Key"})
    return await call_next(request)
