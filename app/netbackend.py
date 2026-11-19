"""网络后端补丁：强制 IPv4 解析。

为什么需要：同一个 CDN 域名往往同时有 A（IPv4）与 AAAA（IPv6）记录，Windows 默认
优先 IPv6。实测（2026-10-26）夸克某直链的 IPv6 路径被压在 ~25Mbps，而其它文件
走同一张网卡能到 12MB/s —— 这类「换条路上限就变了」的情况，只能靠显式选路绕开。

httpx 0.28 的 ``AsyncHTTPTransport`` 不暴露 ``network_backend`` 参数，所以这里在
**httpcore 连接池**上换后端：``pool._network_backend`` 只在新建连接时被读取，
因此补丁只影响后续新建的连接，正在用的连接不受影响。

关键点：只把「解析出来的 IP」换成 IPv4 字面量，TLS 的 ``server_hostname`` 仍由
httpcore 用 URL 里的原始域名传入（在 pool 的 ``start_tls`` 环节），
**SNI 与证书校验保持按域名进行**，不会因为换成 IP 而失效或报证书错误。
"""
from __future__ import annotations

import socket
from typing import Any

_MARK = "_dsh_ipv4_backend"


def is_ip_literal(host: str) -> bool:
    """host 是否已经是 IP 字面量（IPv4 / IPv6）。"""
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return True
        except (OSError, ValueError):
            continue
    return False


def _make_backend(base_cls: type) -> Any:
    """按基类动态生成「只解析 A 记录」的后端。"""
    import anyio

    class _IPv4Backend(base_cls):  # type: ignore[misc, valid-type]
        _dsh_ipv4_backend = True

        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> Any:
            target = host
            if not is_ip_literal(host):
                try:
                    infos = await anyio.to_thread.run_sync(
                        socket.getaddrinfo,
                        host,
                        port,
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                    )
                    if infos:
                        target = infos[0][4][0]
                except (OSError, ValueError):
                    # 解析失败就退回原样，交给 httpx/httpcore 报正常错误
                    target = host
            return await super().connect_tcp(
                target,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )

    return _IPv4Backend()


def install_ipv4_on_transport(transport: Any) -> bool:
    """给 httpx 传输层装 IPv4 后端；成功返回 True。"""
    try:
        import httpcore

        base_cls = httpcore.AnyIOBackend
    except Exception:  # noqa: BLE001 - 没有 httpcore 就没有 httpx 下载
        return False

    pool = getattr(transport, "_pool", None)
    if pool is None or not hasattr(pool, "_network_backend"):
        return False
    try:
        pool._network_backend = _make_backend(base_cls)
    except Exception:  # noqa: BLE001 - 补丁失败就保持双栈，不影响下载
        return False
    return True


def install_ipv4_on_client(client: Any) -> bool:
    """给 ``httpx.AsyncClient`` 装 IPv4 后端（内部就是它的 transport）。"""
    return install_ipv4_on_transport(getattr(client, "_transport", None))


def client_prefers_ipv4(client: Any) -> bool:
    """查询当前客户端是否已装 IPv4 后端（诊断/测试用）。"""
    pool = getattr(getattr(client, "_transport", None), "_pool", None)
    return bool(getattr(getattr(pool, "_network_backend", None), _MARK, False))


def curl_ipv4_options() -> dict:
    """curl_cffi 的「只走 IPv4」选项；不可用时返回空字典。"""
    try:
        from curl_cffi.const import CurlIpResolve, CurlOpt  # type: ignore

        return {CurlOpt.IPRESOLVE: CurlIpResolve.V4}
    except Exception:  # noqa: BLE001
        return {}