"""Windows 系统托盘：关闭主窗时藏到托盘，下载在后台继续。

为什么不用第三方库：pywebview 在 Windows 上本来就把 pythonnet / WinForms 载进来了，
直接拿 `System.Windows.Forms.NotifyIcon` 即可，零新增依赖。

线程模型（很容易踩坑，这里统一收口）：
- `NotifyIcon` 必须创建在**有消息循环的 UI 线程**上，因此所有创建/销毁/更新都通过
  主窗对应的 WinForms `Form.Invoke(...)` marshal 过去；
- 菜单点击回调本身也在 UI 线程执行，但回调里要调 pywebview 的 `show/hide/destroy`
  （它们内部又会 `Invoke`），所以在回调里只起一个短线程去调，避免重入。
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Any, Callable, Optional

# tray.ico 由 assets/ 提供；缺失时用系统默认图标
_ICON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "tray.ico"
)

_MAX_TOOLTIP = 63  # NotifyIcon.Text 的硬限制


def _truncate(text: str, limit: int = _MAX_TOOLTIP) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TrayIcon:
    """把主窗收进系统托盘。仅在 Windows + pywebview(winforms) 下可用。"""

    def __init__(
        self,
        window: Any,
        on_exit: Callable[[], None],
        title: str = "高速下载器",
        tooltip: str = "高速下载器 · 后台运行中",
    ) -> None:
        self.window = window
        self.title = title
        self._on_exit = on_exit
        self._tooltip = _truncate(tooltip)
        self._icon: Any = None
        self._menu: Any = None
        self._balloon_shown = False
        self._error = ""

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return self._icon is not None

    @property
    def error(self) -> str:
        return self._error

    def attach(self) -> bool:
        """在 UI 线程创建托盘图标；失败返回 False，调用方应退化为"关闭即退出"。"""
        if self._icon is not None:
            return True
        if sys.platform != "win32":
            self._error = "非 Windows 平台"
            return False
        if getattr(self.window, "native", None) is None:
            self._error = "窗口后端不是 WinForms"
            return False

        done = threading.Event()

        def build() -> None:
            try:
                self._build()
            except Exception as e:  # noqa: BLE001 - 托盘失败不应影响主流程
                self._error = f"{type(e).__name__}: {e}"
                self._icon = None
            finally:
                done.set()

        if not self._invoke(build):
            return False
        # 从 UI 线程调用时 build 已同步完成；跨线程时等它完成
        done.wait(8)
        return self._icon is not None

    def show_balloon(self, title: str, text: str, once: bool = False) -> None:
        if once and self._balloon_shown:
            return
        self._balloon_shown = True

        def do() -> None:
            try:
                from System.Windows.Forms import ToolTipIcon

                if self._icon is not None:
                    self._icon.ShowBalloonTip(4000, _truncate(title), text, ToolTipIcon.Info)
            except Exception:  # noqa: BLE001
                pass

        self._invoke(do)

    def update_tooltip(self, text: str) -> None:
        text = _truncate(text)
        if text == self._tooltip or self._icon is None:
            return
        self._tooltip = text

        def do() -> None:
            try:
                if self._icon is not None:
                    self._icon.Text = text
            except Exception:  # noqa: BLE001
                pass

        self._invoke(do)

    def dispose(self) -> None:
        """移除托盘图标（不调用会在任务栏留下"幽灵图标"）。"""
        if self._icon is None:
            return

        def do() -> None:
            try:
                if self._icon is not None:
                    self._icon.Visible = False
                    self._icon.Dispose()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._icon = None
                self._menu = None

        self._invoke(do)

    def dispose_direct(self) -> None:
        """在 UI 线程（如 FormClosed 里）直接销毁，避免再往已销毁的窗体 Invoke。"""
        try:
            if self._icon is not None:
                self._icon.Visible = False
                self._icon.Dispose()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._icon = None
            self._menu = None

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _invoke(self, fn: Callable[[], None]) -> bool:
        """把 fn 送到主窗的 UI 线程执行。"""
        form = getattr(self.window, "native", None)
        if form is None:
            return False
        try:
            from System import Action

            form.Invoke(Action(fn))
            return True
        except Exception as e:  # noqa: BLE001
            self._error = f"{type(e).__name__}: {e}"
            return False

    def _build(self) -> None:
        """必须在 UI 线程调用。"""
        import clr  # pythonnet，随 pywebview 一起提供

        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System.Drawing import Icon
        from System.Windows.Forms import (
            ContextMenuStrip,
            NotifyIcon,
            ToolStripMenuItem,
            ToolStripSeparator,
        )

        icon = NotifyIcon()
        icon.Text = self._tooltip
        if os.path.isfile(_ICON_PATH):
            icon.Icon = Icon(_ICON_PATH)

        menu = ContextMenuStrip()
        show_item = ToolStripMenuItem("显示主界面")
        show_item.Click += self._on_menu_show
        quit_item = ToolStripMenuItem("退出下载器")
        quit_item.Click += self._on_menu_quit
        menu.Items.Add(show_item)
        menu.Items.Add(ToolStripSeparator())
        menu.Items.Add(quit_item)

        icon.ContextMenuStrip = menu
        icon.DoubleClick += self._on_menu_show
        icon.Visible = True

        self._icon = icon
        self._menu = menu

    # ---- 菜单回调（UI 线程）----
    def _on_menu_show(self, sender: Any = None, args: Any = None) -> None:
        threading.Thread(target=self._restore, name="tray-restore", daemon=True).start()

    def _on_menu_quit(self, sender: Any = None, args: Any = None) -> None:
        threading.Thread(target=self._quit, name="tray-quit", daemon=True).start()

    # ---- 实际动作（普通线程）----
    def _restore(self) -> None:
        try:
            self.window.show()
            self.window.restore()
        except Exception:  # noqa: BLE001
            pass

    def _quit(self) -> None:
        self.dispose()
        try:
            self._on_exit()
        except Exception:  # noqa: BLE001
            pass
