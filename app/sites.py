"""网盘站点表。

集中维护「域名 → 站点」的归属关系、登录入口、默认 Referer、以及判定
「是否已登录」的标志 Cookie。会话模块和下载引擎共用这张表，避免两处硬编码。
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

SITES: dict[str, dict[str, Any]] = {
    "quark": {
        "name": "夸克网盘",
        "login_url": "https://pan.quark.cn/",
        "referer": "https://pan.quark.cn/",
        "domains": ("quark.cn",),
        # 只要登录态 Cookie（__pus/__puus 是扫码登录后才下发的会话票据）。
        # 别把 __kp/__kps/__ktd/tfstk/isg 这类风控指纹 Cookie 算进来：
        # 它们未登录也会下发，会把「没登录」误判成「已登录」。
        "login_cookies": ("__pus", "__puus"),
        # __puus 是短时效票据，久未访问网盘就会过期：列表还能读，但 CDN 会把请求
        # 当访客（签发 23018 / 下载 412）。取到 Cookie 后先用 __pus 换一张新票据
        # （实现见 app/cookie_refresh.py）。
        "cookie_refresh": {
            "url": "https://drive-pc.quark.cn/1/clouddrive/auth/pc/flush",
            "params": {"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            "cookie_name": "__puus",
            "requires": "__pus",
        },
    },
    "uc": {
        "name": "UC 网盘",
        "login_url": "https://drive.uc.cn/",
        "referer": "https://drive.uc.cn/",
        "domains": ("uc.cn",),
        "login_cookies": ("__pus", "__puus"),
    },
    "baidu": {
        "name": "百度网盘",
        "login_url": "https://pan.baidu.com/",
        "referer": "https://pan.baidu.com/",
        "domains": ("baidu.com",),
        # BDUSS_BFESS 是 BDUSS 的分区变体（新版浏览器会只留后者），两个都认
        "login_cookies": ("BDUSS", "BDUSS_BFESS", "STOKEN"),
    },
    "123": {
        "name": "123 云盘",
        "login_url": "https://www.123pan.com/",
        "referer": "https://www.123pan.com/",
        "domains": ("123pan.com", "123684.com", "123865.com"),
        "login_cookies": ("authorToken",),
    },
    "xunlei": {
        "name": "迅雷云盘",
        "login_url": "https://pan.xunlei.com/",
        "referer": "https://pan.xunlei.com/",
        "domains": ("xunlei.com",),
        # 迅雷登录态靠 cookie + xla2 SDK 本地签名；登录标志以 cookie 出现为准
        "login_cookies": ("user_id", "session"),
    },
    "lanzou": {
        "name": "蓝奏云",
        "login_url": "https://pc.woozooo.com/",
        "referer": "https://pc.woozooo.com/",
        "domains": ("lanzou.com", "lanzous.com", "lanzoui.com", "woozooo.com"),
        # 蓝奏分享免登录，留空即可
        "login_cookies": (),
    },
}


def site_for_host(host: str) -> Optional[str]:
    """按域名反查站点 key；未知域名返回 None。"""
    host = (host or "").strip().lower()
    if not host:
        return None
    # 去掉端口与 IPv6 方括号
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    else:
        host = host.split(":", 1)[0]
    for key, info in SITES.items():
        for domain in info["domains"]:
            if host == domain or host.endswith("." + domain):
                return key
    return None


def site_for_url(url: str) -> Optional[str]:
    try:
        return site_for_host(urlparse(url or "").netloc)
    except ValueError:
        return None


def default_referer(url: str) -> Optional[str]:
    """按直链域名给出该网盘的防盗链 Referer。"""
    key = site_for_url(url)
    return SITES[key]["referer"] if key else None


def site_name(key: str) -> str:
    return (SITES.get(key) or {}).get("name", key)
