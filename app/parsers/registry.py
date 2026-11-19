"""解析器注册表：把「链接 → 解析器」的查找集中在这里。

新增网盘时只需要：在 ``REGISTRY`` 登记对应 ``ShareParser`` 实例；主流程
（Manager / API / 前端）不感知具体网盘。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from ..sites import site_for_url
from .base import ShareParser
from .baidu import BaiduParser
from .lanzou import LanzouParser
from .pan123 import Pan123Parser
from .quark import QuarkParser
from .uc import UcParser
from .xunlei import XunleiParser

#: 已接入分享解析的网盘实例表（按顺序匹配链接；蓝奏域名多放最后避免误匹配）
REGISTRY: list[ShareParser] = [
    QuarkParser(),
    UcParser(),
    BaiduParser(),
    Pan123Parser(),
    XunleiParser(),
    LanzouParser(),
]

#: 识别分享页路径的片段（已知网盘域名上命中这些路径，但还没有解析器时，
#: 给出「接口已预留、待适配」的明确提示，而不是把分享页当成直链去下载）
_SHARE_PATH_FRAGMENTS = ("/s/", "/share/", "/i/", "/b/")


def find_parser(url: str) -> Optional[ShareParser]:
    """按分享链接正则匹配已接入的解析器；未命中返回 None。"""
    for parser in REGISTRY:
        if parser.match(url):
            return parser
    return None


def parser_for_site(site: str) -> Optional[ShareParser]:
    return next((p for p in REGISTRY if p.site_key == site), None)


def _looks_like_share(url: str) -> bool:
    try:
        path = urlparse(url).path
    except ValueError:
        return False
    return any(fragment in path for fragment in _SHARE_PATH_FRAGMENTS)


def classify(url: str) -> dict[str, object]:
    """把输入链接分成三类：``share``（已接入解析器）/ ``unsupported_share`` / ``direct``。

    返回结构::

        {"kind": "share", "parser": ShareParser}
        {"kind": "unsupported_share", "site": "baidu"}
        {"kind": "direct"}
    """
    parser = find_parser(url)
    if parser is not None:
        return {"kind": "share", "parser": parser}
    site = site_for_url(url)
    if site and _looks_like_share(url):
        return {"kind": "unsupported_share", "site": site}
    return {"kind": "direct"}
