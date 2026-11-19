"""网盘分享链接解析器插件包（接口见 ``base.py``，注册表见 ``registry.py``）。"""
from __future__ import annotations

from .base import (
    LoginRequiredError,
    ParseContext,
    ParseError,
    PasscodeRequiredError,
    PasscodeWrongError,
    ResolvedFile,
    ShareFile,
    ShareParser,
    ShareResult,
    ShareUnavailableError,
    UnsupportedShareLinkError,
)
from .registry import classify, find_parser, parser_for_site

__all__ = [
    "LoginRequiredError",
    "ParseContext",
    "ParseError",
    "PasscodeRequiredError",
    "PasscodeWrongError",
    "ResolvedFile",
    "ShareFile",
    "ShareParser",
    "ShareResult",
    "ShareUnavailableError",
    "UnsupportedShareLinkError",
    "classify",
    "find_parser",
    "parser_for_site",
]
