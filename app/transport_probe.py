"""传输层探测：同一个直链、同一套请求头，换不同 TLS/HTTP 栈各打一次。

用途：区分「是凭据/请求头不对」还是「服务端在拦 TLS 指纹」。

- ``httpx``：Python + OpenSSL，也就是下载器现在用的栈；
- ``httpx-chrome-tls``：同样走 OpenSSL，但把 TLS1.2 密码套件顺序改成 Chrome 的顺序
  （JA3 里 ciphers 段能对上，extensions 段仍不同），成本最低的一步尝试；
- ``curl.exe``：Windows 自带，走 **Schannel**（系统原生 TLS 栈），
  和 IDM / 许多原生下载器接近；
- 浏览器栈（WebView2 的 fetch）在 ``app/session.py`` 里做，因为它需要 GUI 会话。

全部只请求 ``Range: bytes=0-1``，不会真的下载文件。
"""
from __future__ import annotations

import os
import shutil
import ssl
import subprocess
from typing import Any

import httpx

# Chrome 桌面版 TLS1.2 密码套件顺序（JA3 的 ciphers 段）
CHROME_CIPHERS = ":".join(
    [
        "ECDHE-ECDSA-AES128-GCM-SHA256",
        "ECDHE-RSA-AES128-GCM-SHA256",
        "ECDHE-ECDSA-AES256-GCM-SHA384",
        "ECDHE-RSA-AES256-GCM-SHA384",
        "ECDHE-ECDSA-CHACHA20-POLY1305",
        "ECDHE-RSA-CHACHA20-POLY1305",
        "ECDHE-RSA-AES128-SHA",
        "ECDHE-RSA-AES256-SHA",
        "AES128-GCM-SHA256",
        "AES256-GCM-SHA384",
        "AES128-SHA",
        "AES256-SHA",
    ]
)


def _chrome_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers(CHROME_CIPHERS)
    except ssl.SSLError:
        pass
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_alpn_protocols(["h2", "http/1.1"])
    except Exception:  # noqa: BLE001
        pass
    return ctx


def probe_httpx(url: str, headers: dict[str, str], timeout: float = 20.0,
                tweak_tls: bool = False) -> dict[str, Any]:
    """用 httpx 打一次；tweak_tls=True 时套用 Chrome 密码套件顺序。"""
    name = "httpx(Chrome TLS)" if tweak_tls else "httpx(当前)"
    req_headers = {**headers, "Range": "bytes=0-1"}
    try:
        kwargs: dict[str, Any] = {"follow_redirects": True, "timeout": timeout}
        if tweak_tls:
            kwargs["verify"] = _chrome_ssl_context()
        with httpx.Client(**kwargs) as client:
            resp = client.get(url, headers=req_headers)
            return {
                "name": name,
                "status": resp.status_code,
                "bytes": len(resp.content[:4096]),
                "content_range": resp.headers.get("content-range", ""),
            }
    except Exception as e:  # noqa: BLE001
        return {"name": name, "status": None, "error": f"{type(e).__name__}: {e}"}


def curl_path() -> str | None:
    found = shutil.which("curl")
    if found:
        return found
    for cand in (
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "curl.exe"),
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Sysnative", "curl.exe"),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def curl_backend() -> str:
    """看一眼 curl.exe 用的是哪个 TLS 后端（首行 --version 里带）。"""
    exe = curl_path()
    if not exe:
        return ""
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=10, errors="replace"
        ).stdout
        for line in out.splitlines():
            if "libcurl" in line:
                return line.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def probe_curl(url: str, headers: dict[str, str], timeout: float = 20.0) -> dict[str, Any]:
    """用 Windows 自带 curl.exe（Schannel）打一次。"""
    name = "curl.exe(Schannel)"
    exe = curl_path()
    if not exe:
        return {"name": name, "status": None, "error": "未找到 curl.exe"}
    cmd = [exe, "-sS", "-o", os.devnull, "-w", "%{http_code}", "-r", "0-1",
           "--max-time", str(int(timeout)), "--http1.1"]
    for key, value in headers.items():
        if key.lower() in ("host", "content-length"):
            continue
        cmd += ["-H", f"{key}: {value}"]
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 8, errors="replace"
        )
        code = (proc.stdout or "").strip().splitlines()
        status = int(code[-1]) if code and code[-1].isdigit() else None
        return {
            "name": name,
            "status": status,
            "error": (proc.stderr or "").strip()[:160] if status is None else "",
        }
    except Exception as e:  # noqa: BLE001
        return {"name": name, "status": None, "error": f"{type(e).__name__}: {e}"}


def probe_curl_cffi(url: str, headers: dict[str, str], timeout: float = 20.0) -> dict[str, Any]:
    """用 curl_cffi（libcurl-impersonate，复现 Chrome TLS 指纹）打一次。"""
    name = "curl_cffi(Chrome)"
    try:
        from curl_cffi import requests as cr
    except Exception as e:  # noqa: BLE001
        return {"name": name, "status": None, "error": f"未安装 curl_cffi：{e}"}
    try:
        resp = cr.get(
            url,
            headers={**headers, "Range": "bytes=0-1"},
            impersonate="chrome",
            timeout=timeout,
        )
        return {
            "name": name,
            "status": resp.status_code,
            "bytes": len(resp.content or b""),
            "content_range": resp.headers.get("content-range") or "",
        }
    except Exception as e:  # noqa: BLE001
        return {"name": name, "status": None, "error": f"{type(e).__name__}: {e}"}


def probe_transports(url: str, headers: dict[str, str], timeout: float = 20.0) -> list[dict[str, Any]]:
    """四种非浏览器传输栈各打一次（浏览器栈由 session 模块负责）。"""
    return [
        probe_httpx(url, headers, timeout, tweak_tls=False),
        probe_httpx(url, headers, timeout, tweak_tls=True),
        probe_curl(url, headers, timeout),
        probe_curl_cffi(url, headers, timeout),
    ]
