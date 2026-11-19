"""暴露给前端 JavaScript 的 pywebview API。

注意：pywebview 6.x 的 js_api 返回值会被 ``json.dumps`` 直接序列化，
**不会**自动解包 ``concurrent.futures.Future``。Manager 的协程跑在独立事件
循环线程里，因此这里每个方法都在 pywebview 分派出来的专用线程上调用
``Future.result()`` 同步取结果，再把可 JSON 序列化的快照/列表返回给前端；
协程内抛出的异常由 ``.result()`` 原样重新抛出，pywebview 会封装成 JS 侧
Promise 的 rejected 结果。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

import webview

from .config import DEFAULT_USER_AGENT
from .curl_parser import parse_curl
from .manager import DownloadManager
from .parsers import (
    LoginRequiredError,
    PasscodeRequiredError,
    PasscodeWrongError,
    ShareUnavailableError,
    UnsupportedShareLinkError,
)
from .session import SessionManager
from .settings import SettingsStore, default_save_dir
from .sites import SITES, site_for_url, site_name


class JsApi:
    def __init__(self, manager: DownloadManager):
        self.m = manager
        self.session = SessionManager()
        # 本地偏好（下载目录、并发数、分块大小），跨启动记住用户选择
        self.settings = SettingsStore.default()
        # 下载引擎通过这个回调向内置会话要凭据（新增 / 重试 / 中途 412 都会用到）
        manager.credential_provider = lambda url: self.session.capture(url)

    # ------------------------------------------------------------------ #
    # 任务操作（Manager 返回 Future，这里同步解包为 JSON 可序列化结果）
    # ------------------------------------------------------------------ #
    def add_download(self, opts: dict[str, Any]):
        opts = dict(opts or {})
        raw_url = (opts.get("url") or "").strip()
        share = opts.get("share") or {}

        # 提交前先查登录态：只有「一个 Cookie 都没有」才算没登录，直接弹登录窗。
        # 已登录但没识别出标志 Cookie 时不能拦 —— 那会把登录好的用户挡在门外。
        site = site_for_url(share.get("share_url") or raw_url)
        if opts.get("auto_credentials", True) and site:
            state = self.session.state("", site)
            if not state.get("logged_in") and not state.get("has_cookie"):
                return self._login_required(
                    site,
                    f"需要先登录{state.get('site_name') or site_name(site)}才能添加任务",
                    auto_open=True,
                )

        try:
            return self.m.add_download(opts).result()
        except LoginRequiredError as e:
            return self._login_required(e.site, str(e), auto_open=True)
        except (ShareUnavailableError, UnsupportedShareLinkError) as e:
            self.m.notify(str(e), "error")
            return {"need_login": False, "error": str(e)}
        except ValueError as e:
            self.m.notify(str(e), "error")
            return {"need_login": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - 桥接层兜底，不把 Python 栈抛给 JS
            self.m.notify(f"添加任务失败：{e}", "error")
            return {"need_login": False, "error": f"添加任务失败：{e}"}

    # ------------------------------------------------------------------ #
    # 链接解析（分享链接 → 文件列表；普通直链原样返回）
    # ------------------------------------------------------------------ #
    def resolve_link(self, opts: dict[str, Any]) -> dict[str, Any]:
        """供前端在输入框粘贴后调用：识别直链 / 分享链接，返回结构化结果。"""
        url = str(opts.get("url") or "").strip()
        if not url:
            return {"kind": "error", "message": "请输入下载链接"}
        try:
            return self.m.resolve_link(opts).result()
        except LoginRequiredError as e:
            payload = self._login_required(e.site, str(e), auto_open=True)
            payload["kind"] = "need_login"
            return payload
        except PasscodeRequiredError as e:
            return {"kind": "passcode_required", "message": str(e)}
        except PasscodeWrongError as e:
            return {"kind": "passcode_wrong", "message": str(e)}
        except UnsupportedShareLinkError as e:
            return {"kind": "error", "code": "unsupported", "message": str(e)}
        except ShareUnavailableError as e:
            return {"kind": "error", "code": "unavailable", "message": str(e)}
        except ValueError as e:
            return {"kind": "error", "message": str(e)}
        except Exception:  # noqa: BLE001 - 桥接层兜底
            return {"kind": "error", "message": "解析失败，请稍后重试"}

    def list_share_dir(self, opts: dict[str, Any]) -> dict[str, Any]:
        """供前端在分享面板点文件夹 / 面包屑时调用：列出该目录下的条目。"""
        if not isinstance(opts, dict) or not opts.get("share_url"):
            return {"kind": "error", "message": "缺少分享链接"}
        try:
            return self.m.list_share_dir(opts).result()
        except LoginRequiredError as e:
            payload = self._login_required(e.site, str(e), auto_open=True)
            payload["kind"] = "need_login"
            return payload
        except PasscodeRequiredError as e:
            return {"kind": "passcode_required", "message": str(e)}
        except PasscodeWrongError as e:
            return {"kind": "passcode_wrong", "message": str(e)}
        except UnsupportedShareLinkError as e:
            return {"kind": "error", "code": "unsupported", "message": str(e)}
        except ShareUnavailableError as e:
            return {"kind": "error", "code": "unavailable", "message": str(e)}
        except ValueError as e:
            return {"kind": "error", "message": str(e)}
        except Exception:  # noqa: BLE001 - 桥接层兜底
            return {"kind": "error", "message": "读取目录失败，请稍后重试"}

    # ------------------------------------------------------------------ #
    # 本地偏好设置（记住上次下载位置 / 并发 / 分块）
    # ------------------------------------------------------------------ #
    def get_settings(self) -> dict[str, Any]:
        return self.settings.values()

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            return self.settings.values()
        return self.settings.update(patch)

    def get_default_dir(self) -> str:
        return default_save_dir()

    # ------------------------------------------------------------------ #
    # 内置会话（WebView2）
    # ------------------------------------------------------------------ #
    def session_sites(self) -> list[dict[str, str]]:
        return self.session.sites()

    def session_state(self, url: str = "", site: str = "") -> dict[str, Any]:
        return self.session.state(url or "", site or "")

    def open_session(self, site: str = "", url: str = "") -> dict[str, Any]:
        site = site or site_for_url(url) or "quark"
        return self.session.open(site)

    def clear_session(self, site: str = "", url: str = "") -> dict[str, Any]:
        site = site or site_for_url(url) or "quark"
        return self.session.clear(site)

    def capture_credentials(self, url: str, site: str = "") -> dict[str, Any]:
        """读取会话凭据；一个 Cookie 都读不到时才顺手打开登录窗口。"""
        site = site or site_for_url(url) or ""
        target = url or (SITES.get(site) or {}).get("login_url", "")
        cred = self.session.capture(target)
        if not cred.get("logged_in") and not cred.get("has_cookie") and site:
            try:
                self.session.open(site)
            except Exception as e:  # noqa: BLE001 - 例如 CLI 模式下没有 GUI
                cred["note"] = f"无法打开登录窗口：{e}"
                return cred
            cred = self.session.capture(target)
        return cred

    # ------------------------------------------------------------------ #
    # 直链诊断
    # ------------------------------------------------------------------ #
    def diagnose_link(self, opts: dict[str, Any]):
        return self.m.probe_link(opts or {}).result()

    def diagnose_task(self, task_id: str):
        """用任务自己的配置诊断：Cookie × Referer 四种组合各打一次。"""
        return self.m.probe_task(task_id).result()

    def transport_probe(self, task_id: str):
        """换传输栈（httpx / 调过密码套件顺序的 httpx / curl.exe-Schannel）各打一次。"""
        return self.m.transport_probe(task_id).result()

    def browser_probe(self, task_id: str):
        """用 WebView2（Chromium 网络栈）在页面上下文里 fetch 同一个直链。"""
        return self.session.browser_probe(self.m.task_url(task_id))

    def apply_probe_fix(self, task_id: str, use_referer: bool = True):
        """按诊断结论调整请求头并立即重试。"""
        return self.m.apply_probe_fix(task_id, bool(use_referer)).result()

    def retry_with_transport(self, task_id: str, kind: str = "curl_cffi"):
        """切换传输后端（curl_cffi = Chrome TLS 指纹）后立即重试。"""
        return self.m.apply_transport(task_id, kind or "curl_cffi").result()

    def site_of_url(self, url: str) -> str:
        key = site_for_url(url or "")
        return (SITES.get(key) or {}).get("name", "")

    def pause_task(self, task_id: str):
        return self.m.pause_task(task_id).result()

    def resume_task(self, task_id: str, opts: dict[str, Any] | None = None):
        """继续/重试；opts 可带 concurrency / prefer_ipv4（界面上顺手热改）。"""
        return self.m.resume_task(task_id, opts or {}).result()

    def set_task_concurrency(self, task_id: str, value: int):
        """下载中热改单个任务的并发数（自适应会被关掉，以手动值为准）。"""
        return self.m.set_task_concurrency(task_id, int(value)).result()

    def cancel_task(self, task_id: str):
        return self.m.cancel_task(task_id).result()

    def remove_task(self, task_id: str, delete_files: bool = False):
        return self.m.remove_task(task_id, bool(delete_files)).result()

    def list_tasks(self):
        return self.m.list_tasks().result()

    # ------------------------------------------------------------------ #
    # 本地对话框 / 文件管理
    # ------------------------------------------------------------------ #
    def browse_folder(self) -> str:
        window = webview.active_window()
        if window is None:
            return ""
        result = window.create_file_dialog(webview.FileDialog.FOLDER)
        if not result:
            return ""
        if isinstance(result, (list, tuple)):
            return result[0]
        return result

    def open_folder(self, path: str) -> bool:
        if not path:
            return False
        try:
            if os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)
            return True
        except OSError:
            return False

    def get_defaults(self) -> dict[str, str]:
        return {"user_agent": DEFAULT_USER_AGENT}

    def parse_curl(self, command: str) -> dict[str, Any]:
        """解析浏览器复制的 cURL 命令，回填表单所需的 URL/头信息。

        解析失败时抛 ValueError，前端 js Promise 会收到错误消息。
        """
        return parse_curl(command or "")

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _login_required(
        self, site: str, message: str, auto_open: bool = True
    ) -> dict[str, Any]:
        """未登录：自动弹出内置登录窗，并把状态回给前端展示引导文案。"""
        if auto_open:
            try:
                self.session.open(site)
            except Exception as e:  # noqa: BLE001 - 开窗失败不阻断，文案里照样引导
                message = f"{message}（登录窗口打开失败：{e}）"
        state = self.session.state("", site)
        return {
            "need_login": True,
            "site": site,
            "site_name": SITES.get(site, {}).get("name") or site_name(site),
            "login_url": SITES.get(site, {}).get("login_url", ""),
            "logged_in": bool(state.get("logged_in")),
            "note": state.get("note", ""),
            "message": message,
        }
