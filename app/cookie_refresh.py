"""网盘会话票据（``__puus``）刷新。

## 为什么需要单独刷新

夸克系网盘（夸克 / UC）的登录态是两层 Cookie：

- ``__pus``：长期会话票据，登录后长期有效；
- ``__puus``：**短时效访问票据**，服务端会在一段时间后作废，浏览器会在访问网盘时
  自动用 ``__pus`` 换一张新的（``auth/pc/flush`` 返回 Set-Cookie）。

下载器只在需要时才打开隐藏读取窗去读 WebView2 的 Cookie。如果用户几天没在应用里
打开过网盘，读到的 ``__puus`` 其实已经过期：列表接口仍能返回数据（它只认 ``__pus``），
但 **CDN 会把这轮请求当成访客** —— 签发直链报 ``23018 download file size limit``，
或者拿到的直链下载时报 ``412`` / ``403 RequestDeniedByCallback``。
浏览器里一切正常，是因为浏览器那侧的票据一直是新的（这也是「同一条链接浏览器能下、
下载器不行」的常见原因）。

所以取到 Cookie 后先用 ``__pus`` 换一张新票据，再拿去解析 / 签发 / 下载。
配置写在 ``app/sites.py`` 的 ``cookie_refresh`` 里，按站点开关。

## 失败策略

刷新只是增强项：网络失败、服务端没下发 Set-Cookie、Cookie 里没有 ``__pus``
（未登录）时**一律原样返回**，绝不因为刷新失败把本来能用的下载挡掉。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

import httpx

from .sites import SITES

# 刷新是下载前的串行步骤，超时给短一点：失败就退回原 Cookie，不要卡住界面
_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


@dataclass
class _RefreshRequest:
    cookie: str
    url: str
    params: dict[str, Any]
    headers: dict[str, str]
    cookie_name: str


def _prepare(site: str, cookie: str) -> Optional[_RefreshRequest]:
    """按站点配置拼请求；不需要刷新时返回 None（一次请求都不发）。"""
    cookie = (cookie or "").strip()
    info = SITES.get(site) or {}
    config = info.get("cookie_refresh") or {}
    if not cookie or not config:
        return None

    required = str(config.get("requires") or "")
    if required and f"{required}=" not in cookie:
        # 没有长期会话票据，说明这条 Cookie 是访客态，刷了也没意义
        return None

    headers = {"Cookie": cookie}
    referer = str(info.get("referer") or "")
    if referer:
        headers["Referer"] = referer
    return _RefreshRequest(
        cookie=cookie,
        url=str(config["url"]),
        params=dict(config.get("params") or {}),
        headers=headers,
        cookie_name=str(config.get("cookie_name") or ""),
    )


def _merge(request: _RefreshRequest, resp: httpx.Response) -> str:
    return merge_set_cookie(
        request.cookie,
        resp.headers.get_list("set-cookie"),
        request.cookie_name,
    )


def refresh_cookie(site: str, cookie: str) -> str:
    """同步刷新（会话线程用）：不需要 / 失败时原样返回。"""
    request = _prepare(site, cookie)
    if request is None:
        return (cookie or "").strip()
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(request.url, params=request.params, headers=request.headers)
    except Exception:  # noqa: BLE001 - 刷新失败不阻断下载
        return request.cookie
    return _merge(request, resp)


async def arefresh_cookie(site: str, cookie: str) -> str:
    """异步刷新（解析器的事件循环里用）：不需要 / 失败时原样返回。"""
    request = _prepare(site, cookie)
    if request is None:
        return (cookie or "").strip()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(request.url, params=request.params, headers=request.headers)
    except Exception:  # noqa: BLE001 - 刷新失败不阻断下载
        return request.cookie
    return _merge(request, resp)


def merge_set_cookie(cookie: str, set_cookies: Iterable[str], name: str) -> str:
    """把 Set-Cookie 里的 ``name=value`` 合并进 Cookie 头（覆盖同名旧值）。

    ``set_cookies`` 原样传 ``httpx`` 的 ``headers.get_list("set-cookie")``：
    每条形如 ``__puus=xxx; Max-Age=86400; Domain=quark.cn; Path=/``，只取分号前那段。
    找不到该名字就原样返回，保证「刷新无效果」不会把已有 Cookie 弄丢。
    """
    if not name:
        return cookie
    new_pair = ""
    for raw in set_cookies or ():
        if not isinstance(raw, str) or not raw.startswith(f"{name}="):
            continue
        new_pair = raw.split(";", 1)[0].strip()
        break
    if not new_pair:
        return cookie

    kept = [
        part.strip()
        for part in (cookie or "").split(";")
        if part.strip() and not part.strip().startswith(f"{name}=")
    ]
    kept.append(new_pair)
    return "; ".join(kept)
