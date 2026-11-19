"""传输后端适配层：httpx（默认）与 curl_cffi（Chrome TLS 指纹）。

为什么需要第二套：
夸克这类网盘 CDN 前面是阿里 Tengine + 风控，可能直接按 **TLS/JA3 指纹** 拦截非浏览器客户端。
httpx 走 Python/OpenSSL，指纹特征明显；`curl_cffi` 内置 libcurl-impersonate，能复现
Chrome 的 TLS 扩展顺序与密码套件，从而绕开这类拦截。

对引擎暴露的接口和 httpx 保持一致的那一小部分：
    await client.aclose()
    async with client.stream("GET", url, headers=..., follow_redirects=True, timeout=...) as r:
        r.status_code / r.headers / async for chunk in r.aiter_bytes(n)
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Optional


class CurlCffiUnavailable(RuntimeError):
    """没装 curl_cffi 时抛出，调用方退化为 httpx。"""


def curl_cffi_available() -> bool:
    try:
        import curl_cffi.requests  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def curl_pool_size(concurrency: int) -> int:
    """curl 后端的句柄数：``AsyncSession`` 默认 ``max_clients=10``，
    不改就等于无论并发设成多少，实际只有 10 条在跑。"""
    try:
        n = int(concurrency)
    except (TypeError, ValueError):
        n = 10
    return max(10, min(256, n))


def _timeout_seconds(timeout: Any) -> Optional[float]:
    """把 httpx.Timeout / float / None 统一成秒数。"""
    if timeout is None:
        return None
    if isinstance(timeout, (int, float)):
        return float(timeout)
    for attr in ("read", "timeout"):
        value = getattr(timeout, attr, None)
        if isinstance(value, (int, float)):
            return float(value)
    return None


class _CurlResponse:
    def __init__(self, raw: Any) -> None:
        self._raw = raw
        self._content: Optional[bytes] = None

    @property
    def status_code(self) -> int:
        return int(self._raw.status_code)

    @property
    def headers(self) -> Any:
        return self._raw.headers

    @property
    def content(self) -> bytes:
        if self._content is not None:
            return self._content
        return self._raw.content or b""

    async def aread(self) -> bytes:
        """对齐 httpx 的 ``await r.aread()``：把响应体读完并返回。"""
        if self._content is None:
            pieces: list[bytes] = []
            async for piece in self._raw.aiter_content(65536):
                if piece:
                    pieces.append(piece)
            self._content = b"".join(pieces)
        return self._content

    async def aiter_bytes(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        async for piece in self._raw.aiter_content(chunk_size):
            if piece:
                yield piece


class _CurlStream:
    """把 curl_cffi 的 ``session.stream(...)`` 包成 httpx 风格的异步上下文。"""

    def __init__(
        self,
        session: Any,
        method: str,
        url: str,
        headers: dict[str, str],
        timeout: Optional[float],
        allow_redirects: bool,
    ) -> None:
        self._session = session
        self._method = method
        self._url = url
        self._headers = headers
        self._timeout = timeout
        self._allow_redirects = allow_redirects
        self._cm: Any = None

    async def __aenter__(self) -> _CurlResponse:
        self._cm = self._session.stream(
            self._method,
            self._url,
            headers=self._headers,
            timeout=self._timeout,
            allow_redirects=self._allow_redirects,
        )
        raw = await self._cm.__aenter__()
        return _CurlResponse(raw)

    async def __aexit__(self, *exc_info: Any) -> Any:
        return await self._cm.__aexit__(*exc_info)


class CurlCffiClient:
    """curl_cffi 异步会话，接口对齐引擎用到的那部分 httpx 行为。"""

    def __init__(
        self,
        impersonate: str = "chrome",
        read_timeout: float = 60.0,
        connect_timeout: float = 15.0,
        prefer_ipv4: bool = False,
        max_clients: int = 10,
    ) -> None:
        try:
            from curl_cffi.requests import AsyncSession
        except Exception as e:  # noqa: BLE001
            raise CurlCffiUnavailable(
                "未安装 curl_cffi：请先 pip install -r requirements.txt"
            ) from e
        self.impersonate = impersonate
        # curl_cffi 的 AsyncSession 默认只放 10 个客户端，不改的话 curl 后端
        # 无论你把并发设成多少，实际都只有 10 条在跑。
        self.max_clients = max(1, int(max_clients or 10))
        # 优先 IPv4：libcurl 的 CURLOPT_IPRESOLVE。某些 CDN 的 IPv6 路径被限速，
        # 换 IPv4 就能跑满（SNI / 证书校验不受影响，仍按域名进行）。
        curl_options: dict[Any, Any] = {}
        if prefer_ipv4:
            from .netbackend import curl_ipv4_options

            curl_options = curl_ipv4_options()
        # 注意：async 版的 AsyncSession 不接受 connect_timeout，只暴露一个总 timeout
        kwargs: dict[str, Any] = {
            "impersonate": impersonate,
            "timeout": read_timeout,
            "max_clients": self.max_clients,
        }
        if curl_options:
            kwargs["curl_options"] = curl_options
        try:
            self._session = AsyncSession(**kwargs)
        except TypeError:
            # 该版本 curl_cffi 不认这些参数：逐个退让，不影响下载
            kwargs.pop("max_clients", None)
            try:
                self._session = AsyncSession(**kwargs)
            except TypeError:
                self._session = AsyncSession(
                    impersonate=impersonate,
                    timeout=read_timeout,
                )

    def stream(
        self,
        method: str,
        url: str,
        headers: Optional[dict[str, str]] = None,
        follow_redirects: bool = True,
        timeout: Any = None,
    ) -> _CurlStream:
        return _CurlStream(
            self._session,
            method,
            url,
            dict(headers or {}),
            _timeout_seconds(timeout),
            follow_redirects,
        )

    async def aclose(self) -> None:
        try:
            await self._session.close()
        except Exception:  # noqa: BLE001
            pass
