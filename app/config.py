"""下载配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional

MB = 1024 * 1024

# 常用桌面浏览器 UA（夸克 CDN 对非常规 UA 更容易触发风控）
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


@dataclass
class DownloadConfig:
    """单个下载任务的全部参数。"""

    url: str
    save_dir: str
    filename: str = ""
    # 并发与分块
    concurrency: int = 64
    chunk_size: int = 4 * MB
    min_chunk_size: int = 2 * MB
    max_chunk_size: int = 5 * MB
    # 反爬 / 风控
    user_agent: str = DEFAULT_USER_AGENT
    referer: str = ""
    # 未显式指定 Referer 时，是否按站点自动补（部分直链对多余 Referer 敏感，可关）
    send_default_referer: bool = True
    cookie: str = ""
    # cURL 导入解析出的其余自定义请求头（覆盖默认指纹中的同名字段）
    extra_headers: dict = field(default_factory=dict)
    # 是否发送 Sec-Ch-Ua / Sec-Fetch-* 等浏览器指纹。用内置会话的真实 UA 时关闭，
    # 避免「UA 说 Edge、客户端提示说 Chrome 130」这种自相矛盾反而被风控。
    use_client_hints: bool = True
    # 注意：本项目的核心是“用大量独立 TCP 连接突破单连接限速”，
    # 因此默认走 HTTP/1.1 + Keep-Alive。HTTP/2 多路复用会把请求合并到
    # 少数连接上，反而共享限速，仅在确认对方按“请求”而非“连接”限速时开启。
    use_http2: bool = False
    # cURL 中 -k / --insecure 对应关闭 TLS 校验，默认开启
    verify_tls: bool = True
    # 传输后端："httpx"（默认，Python/OpenSSL）或 "curl_cffi"（复现 Chrome TLS 指纹，
    # 用于服务端按 JA3 拦截非浏览器客户端的情况）
    transport: str = "httpx"
    # 只解析 A 记录（强制 IPv4）：同一域名 IPv6 路径被限速、IPv4 正常时用。
    # 走 IPv4 只换解析结果，SNI / 证书校验仍按原域名进行。
    prefer_ipv4: bool = False
    # 并发自适应：先小并发起步，按实测吞吐量逐级探到最优并发数再稳住。
    # 关掉就回到「一上来就 cfg.concurrency 路全开」的老行为。
    adaptive_concurrency: bool = True
    # 分享任务续传所需的「逻辑身份」（分享页 URL / stoken / 文件 key 等）。
    # 直链带签名且几小时就失效，靠它才能在续传时重新签发一条。普通直链任务为空。
    share_ref: dict = field(default_factory=dict)
    # 超时与重试
    connect_timeout: float = 15.0
    read_timeout: float = 60.0
    max_retries: int = 12
    # 会话 Cookie 刷新回调：直链下载可能持续数小时，网盘 Cookie 会轮换/过期，
    # 遇到 403/412 时调用它重新从内置会话取一次 Cookie（None 表示无会话可用）。
    cookie_refresher: Optional[Callable[[], str]] = None

    def normalized_chunk_size(self) -> int:
        """把分块大小约束在 2~5MB 区间。"""
        return min(self.max_chunk_size, max(self.min_chunk_size, self.chunk_size))

    @property
    def output_path(self) -> str:
        name = self.filename or "download"
        return os.path.join(self.save_dir, name)
