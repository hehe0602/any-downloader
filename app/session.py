"""内置浏览器会话（WebView2 / Edge 内核）凭据管理。

## 为什么需要它

夸克、UC 等网盘的 CDN 直链在服务端校验登录态：纯 HTTP 请求即使伪造完整浏览器
指纹（UA / Sec-Ch-Ua / Sec-Fetch-* / Referer 全试遍）依然返回 **412**。
原来的变通做法是到 DevTools 里对下载请求 "Copy as cURL" 再粘回来，本模块把这一步
自动化：

1. 下载器内部持有一个 WebView2 会话窗口（Windows 上就是 Edge 内核），扫码登录一次；
2. Cookie 保存在应用自己的数据目录里（`main.py` 用 `private_mode=False` +
   `storage_path` 打开），重启后依然有效；
3. 取凭据时调用 WebView2 的 CookieManager，按**目标直链的 URL** 查询——拿到的
   就是浏览器真正会发出去的那组 Cookie（含 HttpOnly、含父域 `.quark.cn`）。

## 为什么能按任意 URL 查

pywebview 的 `window.get_cookies()` 只按「当前页面 URL」查询
（`GetCookiesAsync(self.url)`）。这里在调用期间临时把查询用的 URL 换成目标地址，
取完立刻还原：整个过程不发任何网络请求，也不改变用户看到的页面。

注意 pywebview 6 的 Windows 后端是两层结构：`BrowserView.instances[uid]` 拿到的是
**BrowserForm**（它的 `url` 只在建窗时赋值、之后没人读），而 `get_cookies()` 真正用的是
**EdgeChrome**（`BrowserForm.browser`）上的 `url`。只改外层等于什么都没改，读到的永远
是「窗口当前页面」的 Cookie —— 站点一旦被重定向（风控页 / 运营商劫持 / 登录跳转），
就会出现「明明登录了却读不到登录态」。所以这里两层都改，并且**一次读多个候选 URL
取并集**（目标直链 / 站点首页 / 站点根域），不把成败押在单个 URL 上。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import webview

from .cookie_refresh import refresh_cookie
from .sites import SITES, site_for_url

# 读一轮 Cookie 的总超时（窗口刚创建时可能需要等页面加载完成，且要串行查几个 URL）。
# 不取更长：这一路径可能被 412 触发的「重取凭据」调用，关窗瞬间若正卡在这里，
# 超时越短进程收尾越快（窗口本身已经先消失）。
_COOKIE_TIMEOUT = 12.0
# 会话窗首次加载的等待上限
_LOAD_TIMEOUT = 20.0
# 隐藏读取窗的加载等待上限（比登录窗短，避免阻塞界面线程太久）
_READER_LOAD_TIMEOUT = 12.0
# 一轮最多查几个候选 URL（目标直链 + 首页 + 各根域）
_MAX_COOKIE_QUERIES = 5


# ---------------------------------------------------------------------- #
# 内部工具
# ---------------------------------------------------------------------- #
def _backend_for(window: Any) -> Any:
    """取 pywebview 的 Windows 后端实例，用于把 Cookie 查询指向任意 URL。"""
    try:
        from webview.platforms import winforms  # type: ignore

        return winforms.BrowserView.instances.get(window.uid)
    except Exception:  # noqa: BLE001 - 非 Windows / 内部结构变化都退化为默认行为
        return None


def _cookie_backends(window: Any) -> list[Any]:
    """返回能临时改「Cookie 查询 URL」的后端对象（按优先级）。

    pywebview 6 的 winforms 后端是两层结构：
    - ``BrowserForm``：``BrowserView.instances[uid]`` 里存的对象，``url`` 只在建窗时赋值；
    - ``EdgeChrome``：``BrowserForm.browser``，``get_cookies()`` 真正读的是它的 ``url``。

    两层都改：新版本靠内层生效，万一 pywebview 内部结构回退也能命中外层。
    """
    backends: list[Any] = []
    form = _backend_for(window)
    if form is None:
        return backends
    for candidate in (getattr(form, "browser", None), form):
        if candidate is not None and hasattr(candidate, "url"):
            backends.append(candidate)
    return backends


def _candidate_urls(site: str, target_url: str) -> list[str]:
    """取 Cookie 时要查的 URL：目标直链 → 站点首页 → 各根域。

    多查几个的原因是 Cookie 的 domain/path 会让「按某个 URL 查」漏掉东西：
    父域 Cookie（``.quark.cn``）在部分 WebView2 版本上对子域查询不返回，
    而登录标志 Cookie 又恰恰都挂在父域上。
    """
    info = SITES.get(site) or {}
    urls: list[str] = []

    def add(url: str) -> None:
        url = (url or "").strip()
        if not url or url in urls or len(urls) >= _MAX_COOKIE_QUERIES:
            return
        urls.append(url)

    add(target_url)
    add(info.get("login_url", ""))
    for domain in info.get("domains", ()):
        add(f"https://{domain}/")
    return urls


def _window_closed(window: Any) -> bool:
    closed = getattr(getattr(window, "events", None), "closed", None)
    try:
        return bool(closed is not None and closed.is_set())
    except Exception:  # noqa: BLE001
        return True


def _cookie_items(raw: Any) -> list[dict[str, Any]]:
    """把 pywebview 返回的 SimpleCookie 列表摊平成字典列表。"""
    items: list[dict[str, Any]] = []
    for cookie in raw or []:
        try:
            for name, morsel in cookie.items():
                items.append(
                    {
                        "name": name,
                        "value": morsel.value,
                        "domain": morsel["domain"] or "",
                        "path": morsel["path"] or "/",
                        "httponly": bool(morsel["httponly"]),
                        "secure": bool(morsel["secure"]),
                    }
                )
        except Exception:  # noqa: BLE001
            continue
    return items


def filter_for_url(items: list[dict[str, Any]], url: str) -> list[dict[str, Any]]:
    """按 RFC 6265 的 domain / path / secure 规则筛出「真会发给该 URL」的 Cookie。

    实测发现 WebView2 的 ``GetCookiesAsync(uri)`` 在某些情况下会返回整个配置
    文件里的 Cookie（不按 host 过滤），所以这一层过滤必须自己做：既保证只把
    目标站点的 Cookie 发给 CDN，也避免把无关站点的登录态带出去。
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return []
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    scheme = (parsed.scheme or "https").lower()

    out: list[dict[str, Any]] = []
    for item in items:
        domain = (item.get("domain") or "").lstrip(".").lower()
        if domain and not (host == domain or host.endswith("." + domain)):
            continue
        cookie_path = item.get("path") or "/"
        if not path.startswith(cookie_path):
            continue
        if item.get("secure") and scheme != "https":
            continue
        out.append(item)
    return out


def cookie_header(items: list[dict[str, Any]]) -> str:
    """拼 `Cookie` 请求头；同名 Cookie 保留 path 更具体的那条。"""
    best: dict[str, dict[str, Any]] = {}
    for item in items:
        name = item.get("name")
        if not name:
            continue
        prev = best.get(name)
        if prev is None or len(item.get("path") or "") >= len(prev.get("path") or ""):
            best[name] = item
    return "; ".join(f"{name}={item['value']}" for name, item in best.items())


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (name, domain, path) 去重：查多个候选 URL 会读到重复 Cookie。"""
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = (
            str(item.get("name") or ""),
            str(item.get("domain") or "").lower(),
            str(item.get("path") or "/"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _empty(site: str = "", note: str = "") -> dict[str, Any]:
    info = SITES.get(site) or {}
    return {
        "site": site,
        "site_name": info.get("name", ""),
        "cookie": "",
        "user_agent": "",
        "referer": info.get("referer", ""),
        "logged_in": False,
        "cookie_count": 0,
        "cookie_names": [],
        "has_cookie": False,
        "window_open": False,
        "note": note,
    }


# ---------------------------------------------------------------------- #
# 会话管理
# ---------------------------------------------------------------------- #
class SessionManager:
    """维护各网盘的登录窗口，并按目标 URL 提取会话凭据。

    两类窗口：
    - **登录窗**（可见）：用户扫码登录用；
    - **读取窗**（隐藏）：仅用来让 WebView2 有一个该站点的文档上下文，从而能读到
      配置目录里已有的 Cookie。它的存在意义是「重启后不用重新打开登录窗」——
      Cookie 持久化在 WebView2 配置里，但必须有窗口才能通过 CookieManager 读出来。
    """

    def __init__(self) -> None:
        self._windows: dict[str, Any] = {}
        self._readers: dict[str, Any] = {}
        self._probes: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._cookie_lock = threading.Lock()

    # ---------------- 站点信息 ----------------
    @staticmethod
    def sites() -> list[dict[str, str]]:
        return [
            {"key": key, "name": info["name"], "login_url": info["login_url"]}
            for key, info in SITES.items()
        ]

    # ---------------- 登录窗口 ----------------
    def open(self, site: str) -> dict[str, Any]:
        """打开（或前置）某网盘的登录窗口。"""
        info = SITES.get(site)
        if info is None:
            raise ValueError(f"未知站点：{site}")
        if getattr(webview, "guilib", None) is None:
            raise RuntimeError(
                "会话功能需要在界面模式下使用；CLI 模式请继续用 --cookie 传会话"
            )
        with self._lock:
            window = self._windows.get(site)
            if window is not None and not _window_closed(window):
                try:
                    window.show()
                    window.restore()
                except Exception:  # noqa: BLE001
                    pass
                return {"site": site, "name": info["name"], "created": False}

            window = webview.create_window(
                f"登录 {info['name']}",
                info["login_url"],
                width=1040,
                height=820,
                min_size=(720, 560),
                background_color="#ffffff",
                text_select=True,
            )
            self._windows[site] = window
            # 等首屏加载完：之后 get_cookies()/evaluate_js 才能立刻返回
            try:
                window.events.loaded.wait(_LOAD_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
            return {"site": site, "name": info["name"], "created": True}

    def _reader(self, site: str) -> Any:
        """拿到一个能读该站点 Cookie 的窗口：优先可见登录窗，否则建一个隐藏窗。"""
        with self._lock:
            for store in (self._windows, self._readers):
                window = store.get(site)
                if window is not None and not _window_closed(window):
                    if store is not self._windows:
                        # 可见登录窗是用户在用的页面，别动它；隐藏读取窗被重定向到站外就拉回来
                        self._ensure_on_site(window, site)
                    return window

            info = SITES[site]
            window = webview.create_window(
                f"会话读取（{info['name']}）",
                info["login_url"],
                width=420,
                height=280,
                hidden=True,
            )
            self._readers[site] = window
            try:
                window.events.loaded.wait(_READER_LOAD_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
            self._ensure_on_site(window, site)
            return window

    def _ensure_on_site(self, window: Any, site: str) -> None:
        """窗口被重定向到站外时（风控页 / 运营商劫持页）把它拉回站点首页。

        只在「当前 URL 明确是别的站点」时动作：空 URL 表示还在加载，交给 loaded 事件；
        同站点（含子域）一律不动，避免打断用户正在看的页面。
        """
        try:
            current = str(window.get_current_url() or "")
        except Exception:  # noqa: BLE001
            return
        if not current.startswith(("http://", "https://")):
            return
        if site_for_url(current) == site:
            return
        try:
            window.load_url(SITES[site]["login_url"])
            window.events.loaded.wait(_READER_LOAD_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass

    def clear(self, site: str) -> dict[str, Any]:
        """关闭该站点的所有窗口并**只**清空该站点的 Cookie。

        注意：pywebview 的 `window.clear_cookies()` 底层是 `DeleteAllCookies()`，
        会把所有网盘的登录态一起清掉（实测踩过：点了「清空」夸克，百度/UC 也得重新登录）。
        所以这里改用 CookieManager 按域名逐条删。
        """
        with self._lock:
            windows = [self._windows.pop(site, None), self._readers.pop(site, None)]
        deleted = 0
        for window in windows:
            if window is None or _window_closed(window):
                continue
            deleted += self._delete_site_cookies(window, site)
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass
        return {"site": site, "cleared": True, "deleted": deleted}

    @staticmethod
    def _delete_site_cookies(window: Any, site: str) -> int:
        """按域名删这个站点的 Cookie（取不到后端 / 调用失败就一条都不删，绝不全局清空）。

        调用要落在 UI 线程上（``CoreWebView2`` 有线程亲和性），实际删除放在
        ``GetCookiesAsync`` 的同步上下文回调里，调用方用信号量等结果。
        """
        domains = tuple((SITES.get(site) or {}).get("domains") or ())
        form = _backend_for(window)
        control = getattr(getattr(form, "browser", None), "webview", None)
        if not domains or form is None or control is None:
            return 0
        try:
            from System import Action, Func, Type
            from System.Collections.Generic import List
            from System.Threading.Tasks import Task, TaskScheduler
            from Microsoft.Web.WebView2.Core import CoreWebView2Cookie
        except Exception:  # noqa: BLE001 - 非 Windows / interop 变了就放弃删除
            return 0

        deleted = {"n": 0}
        urls = _candidate_urls(site, "")
        done = threading.Semaphore(0)

        def on_ui() -> None:
            try:
                manager = control.CoreWebView2.CookieManager
                scheduler = TaskScheduler.FromCurrentSynchronizationContext()
                pending = {"n": len(urls)}

                def make_callback():
                    def callback(task) -> None:
                        try:
                            for cookie in task.Result or []:
                                host = (
                                    str(getattr(cookie, "Domain", "") or "")
                                    .lstrip(".")
                                    .lower()
                                )
                                match = any(
                                    host == d or host.endswith("." + d) for d in domains
                                )
                                if match:
                                    manager.DeleteCookie(cookie)
                                    deleted["n"] += 1
                        except Exception:  # noqa: BLE001
                            pass
                        finally:
                            pending["n"] -= 1
                            if pending["n"] <= 0:
                                done.release()

                    return callback

                for url in urls:
                    manager.GetCookiesAsync(url).ContinueWith(
                        Action[Task[List[CoreWebView2Cookie]]](make_callback()),
                        scheduler,
                    )
            except Exception:  # noqa: BLE001
                done.release()

        try:
            form.Invoke(Func[Type](on_ui))
            done.acquire(timeout=8)
        except Exception:  # noqa: BLE001
            return deleted["n"]
        return deleted["n"]

    # ---------------- 凭据 ----------------
    def capture(self, url: str) -> dict[str, Any]:
        """按目标 URL 提取 Cookie / UA / Referer。

        不弹可见窗口：必要时自动建一个隐藏读取窗，因此「上次登录过、这次刚启动」
        也能直接拿到凭据。
        """
        site = site_for_url(url)
        if site is None:
            return _empty("", "非网盘域名，未注入会话凭据")
        if getattr(webview, "guilib", None) is None:
            return _empty(site, "会话功能仅在界面模式下可用，CLI 请用 --cookie")

        info = SITES[site]
        try:
            window = self._reader(site)
            data = self._collect(site, window, url)
        except Exception as e:  # noqa: BLE001
            return _empty(site, f"读取会话失败：{e}")

        items = data["items"]
        cookie = cookie_header(items)
        # WebView2 的 Cookie 只有在页面访问网盘时才会被服务端续期；应用久未打开过网盘
        # 时读到的 __puus 可能已过期 —— 列表接口仍能用，但 CDN 会把这轮请求当访客
        # （直链 412 / 403、签发 23018）。这里用长期票据 __pus 先换一张新票据。
        cookie = refresh_cookie(site, cookie)
        result = {
            "site": site,
            "site_name": info["name"],
            "cookie": cookie,
            "user_agent": self._user_agent(window),
            "referer": info["referer"],
            # 登录态看站点标志 Cookie：host-only 的登录 Cookie 不会发给 CDN，
            # 但"是否已登录"应以站点首页上是否存在标志 Cookie 为准。
            "logged_in": data["logged_in"],
            "cookie_count": len(items),
            "cookie_names": [i["name"] for i in items][:12],
            "has_cookie": bool(data["raw"]),
            "window_open": True,
            "note": "",
            "extra_headers": self._extra_headers(window, site),
        }
        if not result["logged_in"]:
            result["note"] = self._login_note(site, data["raw"])
        return result

    def state(self, url: str = "", site: str = "") -> dict[str, Any]:
        """概览当前会话状态（必要时建隐藏读取窗，不弹可见窗口）。"""
        site = site or site_for_url(url)
        if site is None:
            return _empty("", "填写网盘直链后会自动匹配站点；也可直接选择站点登录")
        if getattr(webview, "guilib", None) is None:
            return _empty(site, "会话功能仅在界面模式下可用")

        try:
            window = self._reader(site)
            data = self._collect(site, window, url)
        except Exception as e:  # noqa: BLE001
            state = _empty(site, f"读取会话失败：{e}")
            state["window_open"] = True
            return state

        items = data["items"]
        state = _empty(
            site, "" if data["logged_in"] else self._login_note(site, data["raw"])
        )
        state.update(
            {
                "cookie_count": len(items),
                "cookie_names": [i["name"] for i in items][:12],
                "logged_in": data["logged_in"],
                "has_cookie": bool(data["raw"]),
                "window_open": True,
            }
        )
        return state

    def _collect(self, site: str, window: Any, target_url: str) -> dict[str, Any]:
        """读该站点的 Cookie 并判定登录态。

        - ``raw``：本轮读到的全部 Cookie（跨候选 URL 的并集、**未按 URL 过滤**）；
        - ``items``：按目标 URL 过滤后、真正会发给服务端的那一组，用于拼 Cookie 头；
        - ``logged_in``：看标志 Cookie 名，**不看** URL 过滤结果 —— path/domain 过滤
          有可能把标志 Cookie 挡在门外，用它判登录会把已登录误判成未登录。
        """
        info = SITES[site]
        raw = self._read_raw_cookies(window, _candidate_urls(site, target_url))
        items = filter_for_url(raw, target_url or info["login_url"])
        names = {str(i.get("name") or "") for i in raw}
        logged = any(name in info["login_cookies"] for name in names)
        return {"raw": raw, "items": items, "logged_in": logged}

    @staticmethod
    def _login_note(site: str, raw: list[dict[str, Any]]) -> str:
        """未检测到登录态时的引导文案（读到了 Cookie 就别再让用户反复扫码）。"""
        name = (SITES.get(site) or {}).get("name", site)
        if raw:
            return (
                f"未检测到{name}登录标记（已读到 {len(raw)} 个站点 Cookie）；"
                "直接用现有会话重试，若仍返回 412 再点「打开网盘登录」重新扫码"
            )
        return f"未检测到{name}登录态：请点「打开网盘登录」扫码一次"

    # ---------------- 浏览器栈探测 ----------------
    def browser_probe(self, url: str, timeout: float = 20.0) -> dict[str, Any]:
        """用 WebView2（Chromium 网络栈）在页面上下文里 fetch 同一个直链。

        Cookies、TLS、HTTP/2 全部走浏览器自己的实现，因此结果可以和 httpx 直接对比：
        浏览器能过、httpx 过不去 → 服务端在拦 Python 的 TLS 指纹，而不是凭据问题。

        实现：把隐藏窗导航到同源的 ``/robots.txt`` 拿到该 host 的文档 origin
        （同源 fetch 不受 CORS 限制、也不会触发文件下载），再用 ``Range: bytes=0-1`` 取 2 字节。
        """
        name = "WebView2(Chromium)"
        if getattr(webview, "guilib", None) is None:
            return {"name": name, "status": None, "error": "会话功能仅在界面模式下可用"}
        try:
            parsed = urlsplit(url)
        except ValueError:
            return {"name": name, "status": None, "error": "URL 解析失败"}
        host = parsed.netloc
        if not host:
            return {"name": name, "status": None, "error": "URL 缺少主机名"}
        scheme = (parsed.scheme or "https").lower()

        try:
            window = self._probe_window(host, scheme)
        except Exception as e:  # noqa: BLE001
            return {"name": name, "status": None, "error": f"创建探测窗失败：{e}"}

        current = ""
        try:
            current = str(window.get_current_url() or "")
        except Exception:  # noqa: BLE001
            pass
        if host not in current:
            return {
                "name": name,
                "status": None,
                "error": f"探测窗未能停留在该站点（当前 {current[:60]!r}），可能被重定向",
            }

        script = (
            "(() => { window.__bp = 'pending';"
            f" const u = {json.dumps(url)};"
            " fetch(u, {headers: {'Range': 'bytes=0-1'}, credentials: 'include',"
            " cache: 'no-store'})"
            "  .then(async (r) => { let n = 0;"
            "    try { const b = await r.arrayBuffer(); n = b.byteLength; } catch (e) {}"
            "    window.__bp = {status: r.status, len: n,"
            "      cr: r.headers.get('content-range') || '',"
            "      ct: r.headers.get('content-type') || ''}; })"
            "  .catch((e) => { window.__bp = {error: String(e)}; });"
            " return true; })()"
        )
        try:
            window.evaluate_js(script)
        except Exception as e:  # noqa: BLE001
            return {"name": name, "status": None, "error": f"注入检测脚本失败：{e}"}

        deadline = time.monotonic() + timeout
        result: Any = None
        while time.monotonic() < deadline:
            time.sleep(0.25)
            try:
                raw = window.evaluate_js("JSON.stringify(window.__bp || null)")
            except Exception:  # noqa: BLE001
                break
            try:
                data = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                data = None
            if data and data != "pending":
                result = data
                break
        if not isinstance(result, dict):
            return {"name": name, "status": None, "error": "浏览器内请求超时"}
        if "error" in result:
            return {"name": name, "status": None, "error": result["error"]}
        return {
            "name": name,
            "status": result.get("status"),
            "bytes": result.get("len"),
            "content_range": result.get("cr", ""),
        }

    def _probe_window(self, host: str, scheme: str = "https") -> Any:
        with self._lock:
            window = self._probes.get(host)
            if window is not None and not _window_closed(window):
                return window
            window = webview.create_window(
                f"直链探测（{host}）",
                f"{scheme}://{host}/robots.txt",
                width=360,
                height=220,
                hidden=True,
            )
            self._probes[host] = window
            try:
                window.events.loaded.wait(_READER_LOAD_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
            return window

    # ---------------- 内部实现 ----------------
    def _read_raw_cookies(self, window: Any, urls: list[str]) -> list[dict[str, Any]]:
        """加锁把多个候选 URL 的 Cookie 全读出来并取并集。

        整个过程跑在一个独立线程里：``window.get_cookies()`` 会 ``Invoke`` 到 UI 线程
        再阻塞等信号量，页面没加载完时可能很久，不能卡住调用方线程（UI / 下载循环）。

        这里**不做 URL 过滤**，过滤交给调用方（``filter_for_url``），因为「是否登录」
        要看标志 Cookie 名，不能被 path/domain 规则挡掉。
        """
        queries = [u for u in urls if u] or [""]
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                with self._cookie_lock:
                    backends = _cookie_backends(window)
                    saved = [(b, getattr(b, "url", None)) for b in backends]
                    collected: list[Any] = []
                    last_error: Any = None
                    try:
                        for target in queries:
                            # 就是把 GetCookiesAsync 的目标 URI 换掉，不发请求、不动页面
                            for backend, _ in saved:
                                try:
                                    backend.url = target
                                except Exception:  # noqa: BLE001
                                    pass
                            try:
                                collected.extend(window.get_cookies() or [])
                            except Exception as e:  # noqa: BLE001
                                # 单个候选 URL 查失败不算整体失败，其它 URL 的结果照用
                                last_error = e
                    finally:
                        for backend, value in saved:
                            try:
                                backend.url = value
                            except Exception:  # noqa: BLE001
                                pass
                    box["raw"] = collected
                    if not collected and last_error is not None:
                        box["error"] = last_error
            except Exception as e:  # noqa: BLE001
                box["error"] = e

        thread = threading.Thread(target=work, name="session-cookies", daemon=True)
        thread.start()
        thread.join(_COOKIE_TIMEOUT)
        if thread.is_alive():
            raise TimeoutError(
                f"读取 Cookie 超时（{_COOKIE_TIMEOUT:.0f} 秒，登录窗口可能还在加载）"
            )
        if "error" in box:
            raise RuntimeError(str(box["error"]))
        return _dedupe(_cookie_items(box.get("raw")))

    @staticmethod
    def _user_agent(window: Any) -> str:
        try:
            ua = window.evaluate_js("navigator.userAgent")
            return str(ua or "")
        except Exception:  # noqa: BLE001
            return ""

    # ---------------- 非 Cookie 凭据（localStorage 等） ----------------
    @staticmethod
    def _extra_headers(window: Any, site: str) -> dict[str, str]:
        """从页面 localStorage 里取该站点业务 API 需要的非 Cookie 头。

        迅雷云盘的 xla2 SDK 会在 localStorage 里写 ``deviceid`` 与
        ``captcha_Xqp0kJBXWhwaTpB6``，Python 无法离线复现 captcha_sign，
        直接从已加载的页面里读出来复用最稳。
        """
        if site != "xunlei":
            return {}
        try:
            raw = window.evaluate_js(
                "JSON.stringify({d: localStorage.getItem('deviceid')||'', "
                "c: localStorage.getItem('captcha_Xqp0kJBXWhwaTpB6')||''})"
            )
            data = json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            return {}
        headers: dict[str, str] = {}
        did = str(data.get("d") or "").replace("wdi10.", "")
        if did:
            headers["x-device-id"] = did
        cap = str(data.get("c") or "")
        if cap:
            headers["x-captcha-token"] = cap
        return headers
