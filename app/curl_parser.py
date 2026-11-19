r"""浏览器 "Copy as cURL" 命令解析器。

用途：把 DevTools 网络面板里复制下来的完整 cURL 命令，一次性还原成下载任务需要的
URL / UA / Referer / Cookie / 其余请求头，等价于 IDM 拦截浏览器请求时继承的全套头。

支持的常见写法（Chrome / Edge / Firefox 三种 Copy as cURL 风格）：
- 单引号或双引号包裹的参数；
- 行尾反斜杠 ``\`` 换行（类 Unix）与 Windows cmd 的 ``^`` 换行；
- 反斜杠转义（``\"``、``\ `` 等）；
- ``-H / --header``、``-b / --cookie``、``-A / --user-agent``、``-e / --referer``；
- ``-L / --location``、``--http2``、``--http1.1``、``-k / --insecure``、``-o``。

解析不到的、以及下载场景不可复用的选项，统一记入 ``warnings`` 列表提示前端。
"""
from __future__ import annotations

import re
from typing import Any

# HTTP/2 伪头：浏览器 cURL 里可能出现，httpx 会根据 URL 自动生成，不能作为普通头重放
_PSEUDO_HEADERS = {":authority", ":method", ":path", ":scheme", ":status"}

# 下载场景下不可复用、容易引发问题的标准头：
# - Host：由 URL 自动推导；
# - Content-Length：GET 分段下载不应携带；
# - Accept-Encoding：程序强制 identity，避免压缩与 Range 冲突；
# - Connection：由 Keep-Alive 策略接管。
_SKIP_HEADERS = {"host", "content-length", "accept-encoding", "connection"}

# 携带参数值、且解析时直接忽略的短/长选项
_VALUE_OPTIONS_IGNORED = {
    "-m", "--max-time",
    "--connect-timeout",
    "--retry", "--retry-delay",
    "--limit-rate",
    "-w", "--write-out",
    "-z", "--time-cond",
    "-D", "--dump-header",
    "--cacert", "--cert", "--key",
    "--resolve",
}


def _tokenize(command: str) -> list[str]:
    """类 shell 词法切分：保留引号与转义语义，丢弃行续行符。"""
    text = command.strip()
    # 去掉类 Unix 反斜杠续行与 Windows cmd 尖号续行
    text = re.sub(r"\\\s*(?:\r?\n|$)", " ", text)
    text = re.sub(r"\^\s*(?:\r?\n|$)", " ", text)

    tokens: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)

    while i < n:
        ch = text[i]

        if quote:
            # 引号内部的反斜杠转义：常见于 \"、\\、\$
            if ch == "\\" and i + 1 < n:
                nxt = text[i + 1]
                if nxt in ('"', "'", "\\", "$", " ", ";", ",", "="):
                    cur.append(nxt)
                    i += 2
                    continue
                cur.append(ch)
                i += 1
                continue
            if ch == quote:
                quote = None
                i += 1
                continue
            cur.append(ch)
            i += 1
            continue

        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch in (" ", "\t", "\r", "\n"):
            if cur:
                tokens.append("".join(cur))
                cur = []
            i += 1
            continue
        # 引号外的反斜杠转义空格 / 引号
        if ch == "\\" and i + 1 < n and text[i + 1] in (" ", '"', "'", ";"):
            cur.append(text[i + 1])
            i += 2
            continue
        cur.append(ch)
        i += 1

    if cur:
        tokens.append("".join(cur))
    if quote is not None:
        raise ValueError("cURL 命令引号未闭合，请检查复制内容是否完整")
    return tokens


def parse_curl(command: str) -> dict[str, Any]:
    """解析 cURL 命令，返回结构化结果；无法识别 URL 时抛 ValueError。"""
    if not command or not command.strip():
        raise ValueError("请先粘贴 cURL 命令")

    tokens = _tokenize(command)
    if tokens and tokens[0].lower() == "curl":
        tokens = tokens[1:]
    if not tokens:
        raise ValueError("未能从 cURL 命令中识别出参数")

    result: dict[str, Any] = {
        "url": "",
        "user_agent": None,
        "referer": None,
        "cookie": None,
        "extra_headers": {},
        "filename": None,
        "use_http2": None,
        "verify_tls": True,
        "warnings": [],
    }
    warnings: list[str] = result["warnings"]

    i = 0

    def need_value(opt: str) -> str:
        nonlocal i
        if i >= len(tokens):
            raise ValueError(f"选项 {opt} 缺少参数值")
        value = tokens[i]
        i += 1
        return value

    while i < len(tokens):
        tok = tokens[i]
        i += 1

        if tok in ("-H", "--header"):
            header = need_value("-H")
            name, _, value = header.partition(":")
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            low = name.lower()
            if low in _PSEUDO_HEADERS:
                continue
            if low in _SKIP_HEADERS:
                warnings.append(f"已忽略不可复用的头 {name}")
                continue
            if low == "cookie":
                result["cookie"] = value
            elif low == "user-agent":
                result["user_agent"] = value
            elif low == "referer":
                result["referer"] = value
            else:
                result["extra_headers"][name] = value

        elif tok in ("-b", "--cookie"):
            value = need_value("-b")
            if value.startswith("@"):
                warnings.append("--cookie 指向 Cookie 文件，已忽略；请直接粘贴 Cookie 字符串")
            else:
                result["cookie"] = value

        elif tok in ("-A", "--user-agent"):
            result["user_agent"] = need_value("-A")

        elif tok in ("-e", "--referer"):
            result["referer"] = need_value("-e")

        elif tok in ("-o", "--output"):
            result["filename"] = need_value("-o")

        elif tok in ("-O", "--remote-name", "-J", "--remote-header-name"):
            # 程序自行从响应头/URL 推导文件名，这里无需处理
            pass

        elif tok in ("-L", "--location", "--location-trusted"):
            # 后端客户端默认 follow_redirects=True
            pass

        elif tok == "--http2":
            result["use_http2"] = True
        elif tok in ("--http1.1", "--http1.0"):
            result["use_http2"] = False

        elif tok in ("-k", "--insecure"):
            result["verify_tls"] = False

        elif tok in ("-x", "--proxy"):
            need_value("-x")
            warnings.append("代理参数已忽略：当前版本暂不支持 HTTP 代理")

        elif tok == "--compressed":
            # 程序强制 identity 编码，避免压缩与 Range 分块冲突
            pass

        elif tok in ("-d", "--data", "--data-raw", "--data-binary",
                     "--data-ascii", "--form", "-F", "--form-string"):
            need_value(tok)
            warnings.append("POST 请求体已忽略：下载统一使用 GET")

        elif tok in ("-X", "--request"):
            method = need_value("-X").upper()
            if method != "GET":
                warnings.append(f"原始方法 {method} 已忽略：下载统一使用 GET")

        elif tok in _VALUE_OPTIONS_IGNORED:
            need_value(tok)
            warnings.append(f"选项 {tok} 已忽略（不影响下载）")

        elif tok in ("-s", "--silent", "-S", "--show-error", "-v", "--verbose",
                     "-N", "--no-buffer", "-g", "--globoff", "-q", "--disable"):
            pass

        elif tok.startswith("--") and "=" in tok:
            key, _, _ = tok.partition("=")
            warnings.append(f"选项 {key} 已忽略")

        elif tok.startswith("-") and tok != "-":
            warnings.append(f"未识别选项 {tok}，已忽略")

        else:
            if not result["url"]:
                result["url"] = tok
            else:
                warnings.append(f"多余参数已忽略：{tok[:60]}")

    if not result["url"]:
        raise ValueError("cURL 命令里没有找到下载 URL")
    if not result["url"].startswith(("http://", "https://")):
        raise ValueError("URL 不是 http/https 直链：" + result["url"][:80])

    return result
